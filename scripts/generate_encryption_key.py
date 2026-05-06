"""一次性生成 PLATFORM_ENCRYPTION_KEY。

用法：
    python scripts/generate_encryption_key.py

会打印一行 44 字符的 base64 串，复制进 ``.env`` 的 ``PLATFORM_ENCRYPTION_KEY=`` 即可。

⚠️ 务必把它存到密钥管理系统（Vault / KMS）里，**不要跟 DB 备份放一起**。
"""

from cryptography.fernet import Fernet


if __name__ == "__main__":
    print(Fernet.generate_key().decode())
