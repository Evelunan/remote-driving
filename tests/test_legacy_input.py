def test_neutral_input_is_delivered_and_unplug_clears_online_state(legacy_python):
    legacy_python("driving", """
        import asyncio
        import usb1
        from infrastructure.device.usb_connector import USBConnector
        from infrastructure.processing.analog_calculator import AnalogCalculator

        async def check():
            moving = bytes([0, 0, 0, 140, 64])
            neutral = bytes([0, 0, 0, 128, 128])
            packets = iter([moving, neutral])
            class Handle:
                def bulkRead(self, *a, **kw):
                    try:
                        return next(packets)
                    except StopIteration:
                        raise usb1.USBErrorNoDevice()
            usb = USBConnector(1, 2)
            usb.handle = Handle()
            usb._running = True
            received = []
            usb.set_data_callback(received.append)
            await usb._read_loop()
            assert [sample.data for sample in received] == [moving, neutral, None], received
            assert received[0].sampled_at <= received[1].sampled_at
            assert AnalogCalculator().calculate(received[1].data) == {
                'throttle': 0.0, 'brake': 0.0, 'steering': 0.0,
            }
            assert not usb.is_connected()
            assert not usb._should_process(bytes(4), None)
        asyncio.run(check())
    """)


def test_control_queue_keeps_only_latest_sample(legacy_python):
    legacy_python("driving", """
        import asyncio
        from factories.control_factory import ControlFactory
        async def check():
            q = ControlFactory.create_message_queue()
            for n in range(50):
                await q.put({'throttle': n / 100})
            assert await q.get() == {'throttle': 0.49}
            assert await q.is_empty()
        asyncio.run(check())
    """)


def test_usb_close_waits_for_inflight_read(legacy_python):
    legacy_python("driving", """
        import asyncio
        import threading
        import usb1
        from infrastructure.device.usb_connector import USBConnector

        async def check():
            entered = threading.Event()
            released = threading.Event()
            finished = threading.Event()
            class Handle:
                def bulkRead(self, *args, **kwargs):
                    entered.set()
                    assert released.wait(timeout=2)
                    finished.set()
                    raise usb1.USBErrorTimeout()
                def close(self):
                    assert finished.is_set(), 'handle closed during read'
            usb = USBConnector(1, 2)
            usb.handle = Handle()
            usb._running = True
            usb._task = asyncio.create_task(usb._read_loop())
            while not entered.is_set():
                await asyncio.sleep(0.001)
            closing = asyncio.create_task(usb.disconnect())
            await asyncio.sleep(0.01)
            assert not closing.done()
            released.set()
            await asyncio.wait_for(closing, timeout=2)
            assert usb.handle is None and not usb.is_connected()
            await usb.disconnect()
        asyncio.run(check())
    """)
