"""
企业微信消息加解密工具
基于企业微信官方加解密协议，使用 AES-256-CBC + PKCS7
依赖 cryptography 库（python-jose[cryptography] 已引入）
"""
import base64
import hashlib
import struct
import socket
import time
import xml.etree.ElementTree as ET
from typing import Optional

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.backends import default_backend

from app.core.logbuffer import get_logger

logger = get_logger("app.services.wechat_crypto")


class WXBizMsgCrypt:
    """企业微信消息加解密"""

    def __init__(self, token: str, encoding_aes_key: str, corp_id: str):
        self.token = token
        self.corp_id = corp_id
        # EncodingAESKey 43位 → 补= → base64解码 → 32字节 AES Key
        self.aes_key = base64.b64decode(encoding_aes_key + "=")
        # IV 为 AES Key 前16字节
        self.iv = self.aes_key[:16]

    def _sha1_signature(self, *args) -> str:
        """计算 SHA1 签名：对参数排序后拼接取 SHA1"""
        sorted_args = sorted(args)
        raw = "".join(sorted_args)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def verify_signature(self, signature: str, timestamp: str, nonce: str, encrypted: str = "") -> bool:
        """验证消息签名"""
        if not self.token:
            return False
        expected = self._sha1_signature(self.token, timestamp, nonce, encrypted)
        return expected == signature

    def decrypt(self, encrypted: str) -> Optional[tuple]:
        """
        解密消息
        返回 (message_xml, corp_id) 或 None
        """
        try:
            cipher_data = base64.b64decode(encrypted)
            cipher = Cipher(
                algorithms.AES(self.aes_key),
                modes.CBC(self.iv),
                backend=default_backend()
            )
            decryptor = cipher.decryptor()
            plain_padded = decryptor.update(cipher_data) + decryptor.finalize()

            # 去除 PKCS7 填充
            unpadder = sym_padding.PKCS7(128).unpadder()
            plain = unpadder.update(plain_padded) + unpadder.finalize()

            # 解析: random(16) + msg_len(4) + msg + corp_id
            content = plain[16:]
            xml_len = socket.ntohl(struct.unpack("I", content[:4])[0])
            xml_content = content[4:4 + xml_len].decode("utf-8")
            from_corp_id = content[4 + xml_len:].decode("utf-8")

            if from_corp_id != self.corp_id:
                logger.warning(f"消息 CorpID 不匹配: {from_corp_id} != {self.corp_id}")
                return None

            return xml_content, from_corp_id
        except Exception as e:
            logger.warning(f"消息解密失败: {e}")
            return None

    def encrypt(self, reply_msg: str) -> Optional[str]:
        """
        加密回复消息
        返回加密后的 base64 字符串
        """
        try:
            msg_bytes = reply_msg.encode("utf-8")
            corp_bytes = self.corp_id.encode("utf-8")

            import os
            random_bytes = os.urandom(16)
            msg_len = struct.pack("I", socket.htonl(len(msg_bytes)))
            plain = random_bytes + msg_len + msg_bytes + corp_bytes

            # PKCS7 填充
            padder = sym_padding.PKCS7(128).padder()
            padded = padder.update(plain) + padder.finalize()

            cipher = Cipher(
                algorithms.AES(self.aes_key),
                modes.CBC(self.iv),
                backend=default_backend()
            )
            encryptor = cipher.encryptor()
            encrypted = encryptor.update(padded) + encryptor.finalize()

            return base64.b64encode(encrypted).decode("utf-8")
        except Exception as e:
            logger.warning(f"消息加密失败: {e}")
            return None

    def generate_encrypted_reply(self, reply_msg: str) -> Optional[str]:
        """加密回复消息并返回密文（用于回调响应）"""
        return self.encrypt(reply_msg)

    def build_signature(self, timestamp: str, nonce: str, encrypted: str) -> str:
        """生成签名"""
        return self._sha1_signature(self.token, timestamp, nonce, encrypted)

    @staticmethod
    def extract_message(xml_str: str) -> dict:
        """从 XML 中提取消息字段"""
        try:
            root = ET.fromstring(xml_str)
            return {child.tag: child.text or "" for child in root}
        except Exception as e:
            logger.warning(f"XML 解析失败: {e}")
            return {}


def build_text_reply(from_user: str, to_user: str, content: str) -> str:
    """构造回复 XML"""
    return (
        f"<xml>"
        f"<ToUserName><![CDATA[{from_user}]]></ToUserName>"
        f"<FromUserName><![CDATA[{to_user}]]></FromUserName>"
        f"<CreateTime>{int(time.time())}</CreateTime>"
        f"<MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[{content}]]></Content>"
        f"</xml>"
    )
