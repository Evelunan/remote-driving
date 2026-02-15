# send.py
import asyncio
import json
import argparse
import websockets
import cv2
import av
from aiortc import (
    RTCPeerConnection,
    RTCSessionDescription,
    RTCConfiguration,
    RTCIceServer,
    VideoStreamTrack,
)

TURN_SERVER = "47.115.230.126"
# 摄像头轨道
class CameraTrack(VideoStreamTrack):
    kind = "video"

    def __init__(self, index=0):
        super().__init__()
        self.cap = cv2.VideoCapture(index)
        if not self.cap.isOpened():
            raise RuntimeError(f"无法打开摄像头 {index}")

    async def recv(self):
        # 获取下一帧时间戳
        pts, time_base = await self.next_timestamp()

        ret, frame = self.cap.read()
        if not ret:
            await asyncio.sleep(0.01)
            return await self.recv()

        # 将 OpenCV BGR 转成 RGB
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

        # 构造 VideoFrame，格式指定为 rgb24
        f = av.VideoFrame.from_ndarray(frame_rgb, format="rgb24")
        f.pts = pts
        f.time_base = time_base
        return f

    def stop(self):
        if self.cap.isOpened():
            self.cap.release()
        super().stop()


async def main(room, server):
    async with websockets.connect(server) as ws:
        await ws.send(json.dumps({"type": "join", "room": room}))
        my_id, peer_id, pc = None, None, None

        ice_servers = [
            # STUN servers
            RTCIceServer(urls=[
                f"stun:{TURN_SERVER}",
            ]),
            # TURN server
            RTCIceServer(
                urls=[
                    f"turn:{TURN_SERVER}"
                ],
                username="coturn",
                credential="coturn"
            )
        ]



        async def create_pc():
            nonlocal pc
            pc = RTCPeerConnection(
                configuration=RTCConfiguration(
                    iceServers=ice_servers
                )
            )
            pc.addTrack(CameraTrack(0))

            @pc.on("icecandidate")
            async def on_candidate(c):
                if c and peer_id:
                    await ws.send(json.dumps({
                        "type": "candidate",
                        "candidate": {
                            "candidate": c.candidate,
                            "sdpMid": c.sdpMid,
                            "sdpMLineIndex": c.sdpMLineIndex,
                        },
                        "from": my_id,
                        "to": peer_id,
                    }))

            @pc.on("iceconnectionstatechange")
            def on_state():
                print("ICE 状态:", pc.iceConnectionState)

            return pc

        async def send_offer():
            nonlocal pc
            if not pc or not peer_id:
                return
            offer = await pc.createOffer()
            await pc.setLocalDescription(offer)
            await ws.send(json.dumps({
                "type": "offer",
                "sdp": pc.localDescription.sdp,
                "sdpType": pc.localDescription.type,
                "from": my_id,
                "to": peer_id,
            }))
            print("👉 已发送 offer ->", peer_id)

        async for raw in ws:
            data = json.loads(raw)
            t = data.get("type")

            if t == "joined":
                my_id = data["id"]
                peers = data.get("peers", [])
                print("✅ 已加入房间:", my_id, "已有 peers:", peers)
                if peers:
                    peer_id = peers[0]
                    pc = await create_pc()
                    await send_offer()

            elif t == "new_peer":
                new_id = data.get("id")
                print("🆕 新 peer 加入:", new_id)
                peer_id = new_id

                # 关闭旧连接
                if pc:
                    try:
                        await pc.close()
                    except:
                        pass
                    pc = None
                    await asyncio.sleep(0.05)

                pc = await create_pc()
                await send_offer()

            elif t == "answer":
                if data.get("to") == my_id and pc:
                    try:
                        await pc.setRemoteDescription(
                            RTCSessionDescription(sdp=data["sdp"], type=data["sdpType"])
                        )
                        print("✅ 已设置远端描述 (answer)")
                    except Exception as e:
                        print("❌ 设置远端描述失败:", e)

            elif t == "candidate":
                if data.get("to") == my_id and pc:
                    c = data["candidate"]
                    try:
                        from aiortc import RTCIceCandidate
                        await pc.addIceCandidate(RTCIceCandidate(
                            candidate=c["candidate"],
                            sdpMid=c["sdpMid"],
                            sdpMLineIndex=c["sdpMLineIndex"],
                        ))
                    except Exception as e:
                        print("❌ 添加 ICE 候选失败:", e)

            elif t == "peer_left":
                if data.get("id") == peer_id:
                    print("⚠️ 对方离开")
                    peer_id, pc = None, None

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--room", required=True)
    p.add_argument("--server", default=f"ws://{TURN_SERVER}:8765")
    a = p.parse_args()
    asyncio.run(main(a.room, a.server))
