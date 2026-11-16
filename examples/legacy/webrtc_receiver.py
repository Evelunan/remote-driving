import asyncio, json, argparse, websockets, cv2, os
from aiortc import (
    RTCPeerConnection,
    RTCSessionDescription,
    RTCConfiguration,
    RTCIceServer,
    RTCIceCandidate,
)

TURN_SERVER = os.environ.get("RD_TURN_HOST", "127.0.0.1")


async def main(room, server):
    async with websockets.connect(server) as ws:
        await ws.send(json.dumps({"type": "join", "room": room}))
        my_id, peer_id, pc = None, None, None

        ice_servers = [
            # STUN servers
            RTCIceServer(
                urls=[
                    f"stun:{TURN_SERVER}",
                ]
            ),
            # TURN server
            RTCIceServer(
                urls=[f"turn:{TURN_SERVER}"], username=os.environ.get("RD_TURN_USERNAME"), credential=os.environ.get("RD_TURN_PASSWORD")
            ),
        ]

        async def create_pc():
            nonlocal pc
            pc = RTCPeerConnection(
                configuration=RTCConfiguration(iceServers=ice_servers)
            )

            @pc.on("track")
            def on_track(track):
                print("📡 收到轨道:", track.kind)

                async def show():
                    try:
                        while True:
                            frame = await track.recv()
                            img = frame.to_ndarray(format="bgr24")
                            cv2.imshow("Remote", img)
                            if cv2.waitKey(1) & 0xFF == ord("q"):
                                break
                    except Exception as e:
                        print("❌ 播放出错:", e)
                    finally:
                        cv2.destroyAllWindows()

                asyncio.create_task(show())

            @pc.on("icecandidate")
            async def on_candidate(c):
                if c and peer_id:
                    await ws.send(
                        json.dumps(
                            {
                                "type": "candidate",
                                "candidate": {
                                    "candidate": c.candidate,
                                    "sdpMid": c.sdpMid,
                                    "sdpMLineIndex": c.sdpMLineIndex,
                                },
                                "from": my_id,
                                "to": peer_id,
                            }
                        )
                    )

            return pc

        async def handle_offer(sdp, from_id):
            nonlocal peer_id, pc
            peer_id = from_id

            if pc:
                await pc.close()
                pc = None
                await asyncio.sleep(0.05)

            pc = await create_pc()
            await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))

            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)
            await ws.send(
                json.dumps(
                    {
                        "type": "answer",
                        "sdp": pc.localDescription.sdp,
                        "sdpType": pc.localDescription.type,
                        "from": my_id,
                        "to": peer_id,
                    }
                )
            )
            print("✅ 已发送 answer")

        async for raw in ws:
            data = json.loads(raw)
            t = data.get("type")

            if t == "joined":
                my_id = data["id"]
                peers = data.get("peers", [])
                print("✅ 已加入房间:", my_id, "已有 peers:", peers)
                if peers:
                    print("🕓 房间已有 peer，等待 offer...")

            elif t == "offer":
                await handle_offer(data["sdp"], data["from"])

            elif t == "candidate":
                if data.get("to") == my_id and pc:
                    c = data["candidate"]
                    try:
                        await pc.addIceCandidate(
                            RTCIceCandidate(
                                sdpMid=c["sdpMid"],
                                sdpMLineIndex=c["sdpMLineIndex"],
                                candidate=c["candidate"],
                            )
                        )
                    except Exception as e:
                        print("❌ 添加 ICE 候选失败:", e)

            elif t == "peer_left":
                if data.get("id") == peer_id:
                    print("⚠️ 对方离开")
                    peer_id, pc = None, None

            elif t == "new_peer":
                print("🆕 新 peer 加入，等待 offer")
            elif t == "full":
                print(f"full message{data.get("message")}")
                


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--room", required=True)
    p.add_argument("--server", default=f"ws://{TURN_SERVER}:8765")
    a = p.parse_args()
    asyncio.run(main(a.room, a.server))
