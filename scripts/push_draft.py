#!/usr/bin/env python3
"""把排好版的公众号正文 HTML 推进微信草稿箱，手机上打开订阅号助手就能直接群发。

绕开了「手机端粘贴会掉样式」这个死结：正文不经过任何编辑器，直接走 draft/add 接口
落进草稿箱，skill 产出的内联样式一分不掉。

凭证从 credvault 取，不落盘、不进参数。网络出口及 API 地址全部由环境变量配置，
避免把某个公众号账号或网络拓扑固化进通用技能。

用法:
    GZH_WECHAT_API_BASE=... GZH_WECHAT_SOCKS5_PROXY=... push_draft.py 正文.html \
      --title 标题 --appid-credential 凭证名 --appsecret-credential 凭证名 \
      [--author 署名] [--digest 摘要] [--cover 封面图]
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlparse


LOG_PATH = os.environ.get(
    "GZH_WECHAT_LOG_PATH", os.path.join(tempfile.gettempdir(), "gzh-publish.log"))
logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    encoding="utf-8",
    format="%(asctime)s environment=%(env)s module=gzh-publish %(levelname)s %(message)s",
)
logger = logging.LoggerAdapter(logging.getLogger(__name__), {"env": os.environ.get("GZH_ENV", "unknown")})


def die(msg):
    logger.error(msg)
    print(f"✗ {msg}", file=sys.stderr)
    sys.exit(1)


def cred(name):
    """从 credvault 取凭证值；只在进程内存里传递，不打印、不落盘。"""
    r = subprocess.run(["cred", "get", name, "--kind", "apikey", "--raw"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        die(f"取凭证 {name} 失败：{r.stderr.strip()}")
    out = r.stdout.strip()
    # cred --raw 返回的是 {"value":"…"} 包装，不是裸值；兼容两种形态
    try:
        parsed = json.loads(out)
        return parsed["value"] if isinstance(parsed, dict) and "value" in parsed else out
    except json.JSONDecodeError:
        return out


def get_runtime_config():
    """读取运行配置；账号、API 地址和网络出口都必须在代码外指定。"""
    config = {
        "api": os.environ.get("GZH_WECHAT_API_BASE", "").rstrip("/"),
        "proxy": os.environ.get("GZH_WECHAT_SOCKS5_PROXY", ""),
    }
    missing = [name for name in ("GZH_WECHAT_API_BASE", "GZH_WECHAT_SOCKS5_PROXY")
               if not os.environ.get(name)]
    if missing:
        die(f"缺少运行配置：{', '.join(missing)}")
    parsed = urlparse(config["proxy"])
    if parsed.scheme not in {"socks5", "socks5h"} or not parsed.hostname or not parsed.port:
        die("GZH_WECHAT_SOCKS5_PROXY 必须是 socks5://host:port 或 socks5h://host:port")
    return config


def ensure_proxy(config):
    """确认用户已配置的 SOCKS5 出口可用；不启动、重载或修改任何代理。"""
    probe = curl(config, [f"{config['api']}/cgi-bin/token?grant_type=client_credential&appid=probe&secret=probe"])
    if probe.get("errcode") != 40013:
        die(f"配置的 SOCKS5 出口不可用或公众号接口异常：{probe}")
    return config["proxy"]


def curl(config, args, raw=False):
    """所有出网请求统一走 SOCKS 隧道。"""
    cmd = ["curl", "-s", "--proxy", config["proxy"], "--max-time", "30"] + args
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        die(f"请求失败：{r.stderr.strip()}")
    if raw:
        return r.stdout
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        die("公众号接口返回了无法解析的响应")


def check(data, step):
    if data.get("errcode", 0) not in (0,):
        die(f"{step} 失败：errcode={data['errcode']} {data.get('errmsg', '')}")
    return data


def get_token(config, appid, secret):
    d = curl(config, [f"{config['api']}/cgi-bin/token?grant_type=client_credential&appid={appid}&secret={secret}"])
    if d.get("errcode") == 40164:
        die(f"来源 IP 不在白名单内。微信原话：{d.get('errmsg')}\n"
            "  请将 errmsg 中的 IP 加入公众号后台白名单，或检查配置的 SOCKS5 出口。")
    if "access_token" not in d:
        check(d, "换 access_token")
    return d["access_token"]


def upload_cover(config, token, path):
    """封面图必须是微信永久素材，draft/add 的 thumb_media_id 是必填项。"""
    d = check(curl(config, [f"{config['api']}/cgi-bin/material/add_material?access_token={token}&type=image",
                    "-F", f"media=@{path}"]), "上传封面图")
    return d["media_id"]


def upload_inline_images(config, token, html):
    """正文里的外链图会被微信吞掉，必须先转成 mp.weixin.qq.com 的地址。"""
    for src in set(re.findall(r'<img[^>]+src="([^"]+)"', html)):
        if "mp.weixin.qq.com" in src or not os.path.isfile(src):
            continue
        d = check(curl(config, [f"{config['api']}/cgi-bin/media/uploadimg?access_token={token}",
                        "-F", f"media=@{src}"]), f"上传正文图 {src}")
        html = html.replace(src, d["url"])
    return html


def add_draft(config, token, article):
    payload = {"articles": [article]}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
        body = f.name
    try:
        d = check(curl(config, [f"{config['api']}/cgi-bin/draft/add?access_token={token}",
                        "-H", "Content-Type: application/json; charset=utf-8",
                        "--data-binary", f"@{body}"]), "新建草稿")
    finally:
        os.unlink(body)
    return d["media_id"]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("html")
    p.add_argument("--title", required=True)
    p.add_argument("--author", default="")
    p.add_argument("--digest", default="")
    p.add_argument("--cover", help="封面图本地路径；不给则自动生成一张纯色图")
    p.add_argument("--appid-credential", required=True, help="credvault 中 AppID 的凭证名")
    p.add_argument("--appsecret-credential", required=True, help="credvault 中 AppSecret 的凭证名")
    a = p.parse_args()

    if not os.path.isfile(a.html):
        die(f"找不到正文文件：{a.html}")
    with open(a.html, encoding="utf-8") as html_file:
        content = html_file.read().strip()
    config = get_runtime_config()

    print(f"· SOCKS5 出口：{ensure_proxy(config)}")
    token = get_token(config, cred(a.appid_credential), cred(a.appsecret_credential))
    print("· access_token 已获取")

    generated_cover = not a.cover
    cover = a.cover or make_placeholder_cover(a.title)
    try:
        thumb = upload_cover(config, token, cover)
    finally:
        if generated_cover and os.path.exists(cover):
            os.unlink(cover)
    print("· 封面已上传")

    content = upload_inline_images(config, token, content)

    media_id = add_draft(config, token, {
        "title": a.title[:64],
        "author": a.author,
        "digest": a.digest[:120],
        "content": content,
        "content_source_url": "",
        "thumb_media_id": thumb,
        "need_open_comment": 1,
        "only_fans_can_comment": 0,
    })
    logger.info("草稿已推送 media_id=%s title=%s", media_id, a.title[:64])
    print(f"\n✓ 草稿已推送　media_id={media_id}")
    print("  打开手机「订阅号助手」→ 草稿箱，样式和预览页完全一致，确认后直接群发。")


def make_placeholder_cover(title):
    """没给封面时造一张 900x383（公众号封面比例）的占位图，避免接口因缺图报错。"""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (900, 383), "#059669")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/STHeiti Medium.ttc", 54)
    except OSError:
        font = ImageFont.load_default()
    d.text((60, 150), title[:12], fill="#FFFFFF", font=font)
    handle, out = tempfile.mkstemp(prefix="gzh-cover-", suffix=".jpg")
    os.close(handle)
    img.save(out, quality=90)
    return out


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        logger.exception("草稿推送出现未处理异常")
        print(f"✗ 草稿推送异常，详情见日志：{LOG_PATH}", file=sys.stderr)
        sys.exit(1)
