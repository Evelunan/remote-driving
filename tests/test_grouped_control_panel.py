def test_input_feedback_groups_and_readable_chassis_codes(legacy_python):
    legacy_python("driving", """
        import time
        from PyQt6.QtWidgets import QApplication
        from rd_core.samples import ControlInput
        from rd_core.wire import VehicleStatus
        from rd_driving.factories import UIFactory
        from rd_driving.infrastructure.processing.data_processor import LocalInputState
        from rd_driving.ui.widgets.control_panel import ControlPanel
        app = QApplication([])
        panel = ControlPanel()
        panel.resize(300, 600)
        for theme in ('modern_glass', 'dark_tech'):
            panel.apply_theme(UIFactory.set_theme(theme))
            panel.show()
            app.processEvents()
            assert [panel._layout.itemAt(i).widget()._title_text for i in range(4)] == [
                '车辆反馈', '驾驶端输入', '指令确认', '系统连接']
            assert panel._scroll_area.widget() is panel._panel
            assert not panel._vehicle_details.isVisible()
            for code, label in ((1, 'D 前进（1）'), (2, 'N／停止（2）'),
                                (3, 'R 倒退（3）'), (0, '未知（0）'), (99, '未知（99）')):
                panel.set_vehicle_status(VehicleStatus(shift_level=code))
                assert panel._vehicle_stats['shift_level'].get_value() == label
            panel.set_vehicle_status(VehicleStatus(shift_level=None))
            assert panel._vehicle_stats['shift_level'].get_value() == '--'
        state = LocalInputState(True, ControlInput(-0.75, 0, -0.25,
                                time.monotonic(), time.time_ns() // 1_000_000))
        panel.set_local_input(state)
        assert panel._gear.get_value() == 'R 倒挡'
        assert {key: item.get_value() for key, item in panel._input_stats.items()} == {
            'throttle': '75', 'brake': '0', 'steering': '-25'}
        # Confirmed commands and local input can hold different samples.
        panel.set_command_status(10, 0, 20, 35)
        assert panel._vehicle_card.isAncestorOf(panel._vehicle_stats['speed_mps'])
        assert panel._input_card.isAncestorOf(panel._input_stats['throttle'])
        assert panel._confirmation_card.isAncestorOf(panel._confirmation_state)
        assert panel._confirmation_card.isAncestorOf(panel._command_stats['elapsed'])
        assert panel._vehicle_details.isAncestorOf(panel._command_stats['throttle'])
        assert panel._vehicle_details.isAncestorOf(panel._measurement_note)
        assert panel._input_stats['throttle'].get_value() == '75'
        panel._vehicle_details_button.click()
        assert panel._vehicle_details.isVisible()
        panel.set_local_input(None)
        assert all(item.get_value() == '--' for item in panel._input_stats.values())
        assert panel._confirmation_state.get_value() == '接口已确认'
        panel.close()
    """)


def test_dashboard_sidebar_orders_metrics_connections_and_disclosures(legacy_python):
    legacy_python("driving", """
        import os
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        from PyQt6.QtCore import QPoint, QRect, Qt
        from PyQt6.QtTest import QTest
        from PyQt6.QtWidgets import QApplication, QPushButton
        from rd_driving.factories import UIFactory
        from rd_driving.ui.widgets.control_panel import ControlPanel
        app = QApplication([])
        app.setStyle('Fusion')
        panel = ControlPanel()
        panel.resize(328, 652)

        def bounds(widget, parent):
            return QRect(widget.mapTo(parent, QPoint()), widget.size())

        for theme in ('modern_glass', 'dark_tech'):
            panel.apply_theme(UIFactory.set_theme(theme))
            panel.show()
            app.processEvents()
            assert panel._camera_details.isHidden()
            assert panel._vehicle_details.isHidden()

            vehicle = panel._vehicle_card
            stats = panel._vehicle_stats
            speed, gear, steering = [
                bounds(stats[key], vehicle)
                for key in ('speed_mps', 'shift_level', 'steering_deg')
            ]
            brake, autodrive = [
                bounds(stats[key], vehicle) for key in ('p_status', 'is_autodrive')
            ]
            assert speed.right() < gear.left() and gear.right() < steering.left()
            assert max(speed.bottom(), gear.bottom(), steering.bottom()) < min(
                brake.top(), autodrive.top()
            )
            assert brake.right() < autodrive.left()
            assert abs(brake.width() - autodrive.width()) <= 1

            connections = panel.get_connection_items()
            expected = ('usb', 'quic', 'video', 'control', 'webrtc', 'signal')
            assert tuple(connections) == expected
            assert panel.layout().itemAt(0).widget() is panel._action_area
            assert panel._action_area.accessibleName() == '会话操作'
            for key in ('video', 'control'):
                button = panel._action_buttons[key]
                assert panel._action_area.isAncestorOf(button)
                assert not connections[key].findChildren(QPushButton)
                assert button.accessibleName() == button.text()
                assert button.focusPolicy() & Qt.FocusPolicy.TabFocus
                assert button.height() >= 44
            for key in ('signal', 'webrtc', 'quic', 'usb'):
                assert connections[key].isAncestorOf(panel._action_buttons[key])
            viewport = panel._scroll_area.viewport()
            rows = [bounds(connections[key], viewport) for key in expected]
            assert all(first.bottom() < second.top() for first, second in zip(rows, rows[1:]))
            assert all(viewport.rect().contains(row) for row in rows)

            diagnosis = bounds(panel._vehicle_details_button, viewport)
            video_stats = bounds(panel._camera_details_button, viewport)
            assert rows[-1].bottom() < diagnosis.top() == video_stats.top()
            assert diagnosis.right() < video_stats.left()
            assert viewport.rect().contains(diagnosis)
            assert viewport.rect().contains(video_stats)

        panel.set_camera_grid('front', 24, 1280, 720, 45)
        panel.set_camera_grid('back', 0, 0, 0)
        panel._camera_details_button.click()
        assert panel._camera_details.isVisible()
        actions = bounds(panel._action_area, panel)
        scroll = panel._scroll_area.verticalScrollBar()
        scroll.setValue(scroll.maximum())
        app.processEvents()
        assert bounds(panel._action_area, panel) == actions
        assert panel.rect().contains(actions)
        clicks = []
        panel.video_toggle_requested.connect(lambda: clicks.append('video'))
        panel.control_toggle_requested.connect(lambda: clicks.append('control'))
        panel.set_video_requested(True)
        panel.set_control_action(True)
        video, control = (panel._action_buttons[key] for key in ('video', 'control'))
        assert video.accessibleName() == '断开视频' and control.accessibleName() == '停止控制'
        video.setFocus()
        QTest.keyClick(video, Qt.Key.Key_Space)
        QTest.keyClick(video, Qt.Key.Key_Tab)
        assert control.hasFocus()
        QTest.keyClick(control, Qt.Key.Key_Space)
        assert clicks == ['video', 'control']
        assert [card._title_text for card in panel.camera_cards.values()] == ['前视', '后视', '左视', '右视']
        assert [item.get_value() for item in panel.camera_data['front'].values()] == ['24', '1280×720', '45']
        assert all(item.get_value() == '--' for item in panel.camera_data['back'].values())
        panel._camera_details_button.click()
        assert panel._camera_details.isHidden()
        panel.close()
    """)


def test_buttons_share_interaction_colors_and_session_state(legacy_python):
    legacy_python("driving", """
        import os
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        from PyQt6.QtCore import QPoint, Qt
        from PyQt6.QtTest import QTest
        from PyQt6.QtWidgets import QApplication
        from rd_driving.factories import UIFactory

        app = QApplication([])
        def fill(button):
            app.processEvents()
            image = button.grab().toImage()
            return image.pixelColor(image.width() // 2, int(5 * image.devicePixelRatio())).name().upper()

        for theme_name in ("dark_tech", "modern_glass"):
            theme = UIFactory.set_theme(theme_name)
            panel = UIFactory.create_control_panel()
            panel.resize(328, 950)
            panel.show()
            panel.set_control_action(False)
            video, control, reconnect = (panel._action_buttons[key] for key in ("video", "control", "quic"))
            disclosure = panel._vehicle_details_button
            for button in (video, control, reconnect, disclosure):
                QTest.mouseMove(panel, QPoint(0, 0))
                assert fill(button) == theme.bg_tertiary
                QTest.mouseMove(button, button.rect().center())
                assert fill(button) == theme.button_hover
                QTest.mousePress(button, Qt.MouseButton.LeftButton)
                assert fill(button) == theme.primary_dark
                QTest.mouseRelease(button, Qt.MouseButton.LeftButton)
                if button is disclosure:
                    assert button.isChecked() and fill(button) == theme.primary_glow
                else:
                    assert not button.isChecked() and fill(button) == theme.button_hover
                QTest.mouseMove(panel, QPoint(0, 0))
                assert fill(button) == (theme.primary if button is disclosure else theme.bg_tertiary)
                button.setDisabled(True)
                QTest.mouseMove(button, button.rect().center())
                assert fill(button) == theme.bg_primary
                button.setEnabled(True)
            for button, set_requested in ((video, panel.set_video_requested), (control, panel.set_control_action)):
                QTest.mouseMove(panel, QPoint(0, 0))
                set_requested(True)
                assert button.isChecked() and fill(button) == theme.primary
                QTest.mouseMove(button, button.rect().center())
                assert fill(button) == theme.primary_glow
                QTest.mousePress(button, Qt.MouseButton.LeftButton)
                assert fill(button) == theme.primary_dark
                QTest.mouseRelease(button, Qt.MouseButton.LeftButton)
                assert button.isChecked()
                set_requested(False)
                assert not button.isChecked() and fill(button) == theme.button_hover
            panel.close()
    """)


def test_input_ack_and_feedback_have_independent_original_deadlines(legacy_python):
    legacy_python("driving", """
        import time
        from types import SimpleNamespace
        from PyQt6.QtWidgets import QApplication
        from rd_core.samples import ControlInput
        from rd_core.wire import VehicleStatus
        from rd_driving.main_window import MainWindow
        from rd_driving.infrastructure.processing.data_processor import LocalInputState
        from rd_driving.infrastructure.workers.control_system_thread import ControlSystemThread
        app = QApplication([])
        window = MainWindow()
        window._setup_control_page()
        window._setup_signals()
        window._services_running = True
        panel = window._control_panel
        worker = ControlSystemThread(window._bridge, window.settings)
        window._bridge.quic_status_changed.emit(True, '已认证')
        window._bridge.control_status_changed.emit('active', '控制已授权')
        def state(age=0, brake=0):
            return LocalInputState(True, ControlInput(-0.75 if not brake else 0, brake, -0.25,
                                   time.monotonic() - age, time.time_ns() // 1_000_000))
        def feedback():
            worker._vehicle_status(SimpleNamespace(payload=VehicleStatus(
                speed_mps=0, shift_level=2, p_status=False), source_time_ms=1234),
                time.monotonic() + 0.3)
        # No connection, lease or execution is needed to show local input.
        fresh = state()
        window._bridge.local_input_changed.emit(fresh)
        original_deadline = fresh.sample.sampled_at + 0.2
        assert window._local_input_deadline == original_deadline
        assert panel._input_stats['throttle'].get_value() == '75'
        old = ControlInput(0.1, 0, 0.2, time.monotonic() - 0.05, 1234)
        worker._execution(old, None, 60)
        assert window._command_deadline == old.sampled_at + 0.3
        assert panel._command_stats['throttle'].get_value() == '10'
        assert panel._input_stats['throttle'].get_value() == '75'
        feedback()
        # Stopped/N and p_status false are valid, not mismatches with local R.
        assert panel._vehicle_stats['shift_level'].get_value() == 'N／停止（2）'
        assert panel._vehicle_note.text() == '反馈：实时，部分字段未知'
        window._command_deadline = time.monotonic() - 1
        window._expire_telemetry()
        assert panel._confirmation_state.get_value() == '确认已过期'
        assert panel._input_stats['throttle'].get_value() == '75'
        window.video_gate.invalidate()
        worker._execution(old, None, 60)
        assert window._command_deadline == old.sampled_at + 0.3
        assert panel._confirmation_state.get_value() == '接口已确认'
        assert panel._vehicle_stats['speed_mps'].get_value() == '0'
        window._on_quic_status_changed(False, '离线')
        assert panel._vehicle_stats['speed_mps'].get_value() == '--'
        assert panel._input_stats['throttle'].get_value() == '75'
        # Qt queuing never grants old input or ACKs a fresh lifetime.
        worker._execution(ControlInput(0.9, 0, 0, time.monotonic() - 1, 1234), None, 1)
        assert panel._command_stats['throttle'].get_value() == '--'
        window._bridge.local_input_changed.emit(state(age=1))
        assert panel._input_stats['throttle'].get_value() == '--'
        window._bridge.local_input_changed.emit(state(brake=1))
        assert panel._input_stats['brake'].get_value() == '100'
        feedback()
        window._local_input_deadline = time.monotonic() - 1
        window._expire_telemetry()
        assert panel._input_stats['brake'].get_value() == '--'
        assert panel._vehicle_stats['speed_mps'].get_value() == '0'
        window._bridge.local_input_changed.emit(state())
        window._vehicle_status_deadline = time.monotonic() - 1
        window._expire_telemetry()
        assert panel._vehicle_stats['speed_mps'].get_value() == '--'
        assert panel._input_stats['throttle'].get_value() == '75'
        window._on_usb_status_changed(False, '未连接')
        assert panel._input_stats['throttle'].get_value() == '--'
        assert window._local_input_deadline is None
        window._service_generation += 1
        window._bridge.local_input_changed.emit(state())
        assert panel._input_stats['throttle'].get_value() == '--'
        worker.request_stop()
        window.close()
        app.processEvents()
    """)
