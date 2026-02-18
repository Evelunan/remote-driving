import asyncio
from factories import VideoFactory
from config.loaders import get_quic_config, get_webrtc_config


async def test_video():
    quic_config = get_quic_config()
    webrtc_config = get_webrtc_config()
    video = VideoFactory.create_video_receiver(
        room="1234",
        server_url=webrtc_config.signaling.url,
        ice_servers=webrtc_config.ice_servers,
    )
    await video.start()

if __name__ == "__main__":
    asyncio.run(test_video())