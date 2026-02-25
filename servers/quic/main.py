import asyncio
from pathlib import Path
from aioquic.asyncio import serve
from aioquic.quic.configuration import QuicConfiguration
from services import RelayProtocol

from common.constants import CERTS_PATH
from config.loaders import get_quic_config
from logger import get_logger

logger = get_logger(__name__)


async def main():
    quic_config = QuicConfiguration(is_client=False)
    cert_file = CERTS_PATH / "cert.pem"
    key_file = CERTS_PATH / "key.pem"

    if not cert_file.exists() or not key_file.exists():
        logger.info(f"证书文件或私钥文件不存在，请检查{CERTS_PATH}")

    config = get_quic_config()

    quic_config.load_cert_chain(certfile=cert_file, keyfile=key_file)
    quic_config.enable_datagrams = config.enable_datagrams
    quic_config.max_datagram_frame_size = config.max_datagram_frame_size
    quic_config.idle_timeout = config.idle_timeout

    await serve(
        host=config.host,
        port=config.port,
        configuration=quic_config,
        create_protocol=RelayProtocol,
    )

    logger.info(f"中继服务器正在监听 {config.host}:{config.port}")
    await asyncio.Future()  # 永久运行


if __name__ == "__main__":
    asyncio.run(main())
