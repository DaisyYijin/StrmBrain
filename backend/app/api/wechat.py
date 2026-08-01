"""
企业微信自建应用回调接口
- GET  /api/wechat/callback  : URL 验证（企业微信后台配置回调时调用）
- POST /api/wechat/callback  : 接收用户消息（用户在应用中发消息时调用）
"""
from fastapi import APIRouter, Request, Query
from fastapi.responses import PlainTextResponse, Response
from app.schemas import ApiResponse
from app.core.logbuffer import get_logger

logger = get_logger("app.api.wechat")

router = APIRouter(prefix="/api/wechat", tags=["wechat"])


def _get_crypto():
    """构建加解密实例，未配置时返回 None"""
    from app.services.wechat_app import WeChatAppService
    if not WeChatAppService.is_callback_configured():
        return None
    c = WeChatAppService._get_config()
    from app.services.wechat_crypto import WXBizMsgCrypt
    return WXBizMsgCrypt(
        token=c["callback_token"],
        encoding_aes_key=c["callback_aes_key"],
        corp_id=c["corp_id"],
    )


@router.get("/callback", response_class=PlainTextResponse)
async def wechat_verify(
    request: Request,
    msg_signature: str = Query(...),
    timestamp: str = Query(...),
    nonce: str = Query(...),
    echostr: str = Query(...),
):
    """
    企业微信回调 URL 验证
    企业微信后台配置「接收消息」时，会向此地址发送 GET 请求验证
    需验签后解密 echostr 并返回明文
    """
    crypto = _get_crypto()
    if not crypto:
        logger.warning("企业微信回调未配置 Token/EncodingAESKey")
        return PlainTextResponse("callback not configured", status_code=503)

    # 验签
    if not crypto.verify_signature(msg_signature, timestamp, nonce, echostr):
        logger.warning(f"企业微信回调验签失败: sig={msg_signature}")
        return PlainTextResponse("signature verify failed", status_code=403)

    # 解密 echostr
    result = crypto.decrypt(echostr)
    if result is None:
        logger.warning("企业微信回调 echostr 解密失败")
        return PlainTextResponse("decrypt failed", status_code=500)

    echo_plain, _ = result
    logger.info("企业微信回调 URL 验证成功")
    return PlainTextResponse(echo_plain)


@router.post("/callback", response_class=PlainTextResponse)
async def wechat_message(
    request: Request,
    msg_signature: str = Query(...),
    timestamp: str = Query(...),
    nonce: str = Query(...),
):
    """
    企业微信消息接收
    用户在应用中发送消息时，企业微信会向此地址 POST 加密 XML
    """
    crypto = _get_crypto()
    if not crypto:
        return PlainTextResponse("callback not configured", status_code=503)

    # 读取加密的 XML body
    body = await request.body()
    body_text = body.decode("utf-8")

    # 从 XML 中提取 Encrypt 字段
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(body_text)
        encrypt_elem = root.find("Encrypt")
        if encrypt_elem is None or not encrypt_elem.text:
            logger.warning("企业微信回调 XML 中未找到 Encrypt 字段")
            return PlainTextResponse("success")
        encrypted = encrypt_elem.text
    except Exception as e:
        logger.error(f"企业微信回调 XML 解析失败: {e}")
        return PlainTextResponse("success")

    # 验签
    if not crypto.verify_signature(msg_signature, timestamp, nonce, encrypted):
        logger.warning("企业微信消息验签失败")
        return PlainTextResponse("success")

    # 解密
    result = crypto.decrypt(encrypted)
    if result is None:
        logger.warning("企业微信消息解密失败")
        return PlainTextResponse("success")

    xml_content, _ = result

    # 解析消息
    from app.services.wechat_crypto import WXBizMsgCrypt, build_text_reply
    msg = WXBizMsgCrypt.extract_message(xml_content)

    msg_type = msg.get("MsgType", "")
    content = msg.get("Content", "").strip()
    from_user = msg.get("FromUserName", "")
    to_user = msg.get("ToUserName", "")

    logger.info(f"企业微信收到消息: type={msg_type}, content={content}, from={from_user}")

    # 仅处理文本消息
    if msg_type != "text":
        reply_text = "目前仅支持文本消息，请发送文字指令。输入「帮助」查看可用命令。"
    else:
        from app.services.wechat_app import WeChatAppService
        reply_text = await WeChatAppService.handle_command(content, from_user)

    # 构造加密回复
    reply_xml = build_text_reply(from_user, to_user, reply_text)
    encrypted_reply = crypto.encrypt(reply_xml)
    if encrypted_reply:
        reply_signature = crypto.build_signature(timestamp, nonce, encrypted_reply)
        response_xml = (
            f"<xml>"
            f"<Encrypt><![CDATA[{encrypted_reply}]]></Encrypt>"
            f"<MsgSignature><![CDATA[{reply_signature}]]></MsgSignature>"
            f"<TimeStamp>{timestamp}</TimeStamp>"
            f"<Nonce><![CDATA[{nonce}]]></Nonce>"
            f"</xml>"
        )
        return Response(content=response_xml, media_type="application/xml")

    return PlainTextResponse("success")


@router.get("/callback-url", response_model=ApiResponse)
async def get_callback_url(request: Request):
    """获取回调 URL（供前端显示）"""
    # 尝试从请求中获取 host
    base_url = str(request.base_url)
    callback_url = f"{base_url}api/wechat/callback"
    return ApiResponse(data={"callback_url": callback_url})
