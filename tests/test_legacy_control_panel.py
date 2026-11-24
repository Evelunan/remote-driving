def test_panel_distinguishes_targets_from_unavailable_odometry(legacy_python):
    legacy_python("driving", """
        import os
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        from PyQt6.QtWidgets import QApplication, QLabel
        from rd_driving.ui.widgets.control_panel import ControlPanel
        app = QApplication([])
        panel = ControlPanel()
        assert all(item.get_value() == '--' for item in panel._command_stats.values())
        panel.set_command_status(50, 0, -20, 35)
        assert panel._command_stats['throttle'].get_value() == '50'
        assert panel._command_stats['elapsed'].get_value() == '35'
        assert any('反馈未接入' in item.text() for item in panel.findChildren(QLabel))
        assert all(item.get_value() == '--' for item in panel._vehicle_stats.values())
        panel.clear_command_status()
        assert all(item.get_value() == '--' for item in panel._command_stats.values())
        panel.close()
    """)


def test_local_gear_visible_without_vehicle_feedback(legacy_python):
    legacy_python("driving", """
        import os
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        from PyQt6.QtWidgets import QApplication
        from rd_driving.main_window import MainWindow
        from rd_driving.infrastructure.processing.data_processor import LocalInputState
        from rd_core.samples import ControlInput
        import time

        app = QApplication([])
        window = MainWindow()
        window._setup_control_page()
        window._setup_signals()
        window._services_running = True
        panel = window._control_panel
        def displayed():
            return panel._gear.get_value(), panel._braking.get_value()
        def state(reverse, braking):
            return LocalInputState(reverse, ControlInput(0, int(braking), 0,
                                   time.monotonic(), time.time_ns() // 1_000_000))

        assert displayed() == ('--', '--')
        window._bridge.local_input_changed.emit(state(False, False))
        assert displayed() == ('D 前进挡', '未触发')
        window._bridge.local_input_changed.emit(state(True, True))
        window._bridge.control_confirmation_expired.emit()
        assert displayed() == ('R 倒挡', '已触发')
        window._bridge.local_input_changed.emit(state(True, False))
        assert displayed() == ('R 倒挡', '未触发')
        window._bridge.usb_status_changed.emit(False, '未连接')
        assert displayed() == ('--', '--')
        window.close()
        app.processEvents()
    """)
