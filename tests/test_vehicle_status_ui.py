def test_chassis_display_units_unknown_fields_and_details(legacy_python):
    legacy_python("driving", """
        from PyQt6.QtWidgets import QApplication
        from rd_core.wire import VehicleStatus
        from rd_driving.ui.widgets.control_panel import ControlPanel
        app = QApplication([])
        panel = ControlPanel()
        assert panel._vehicle_details.isHidden()
        panel._vehicle_details_button.click()
        assert not panel._vehicle_details.isHidden()
        panel.set_vehicle_status(VehicleStatus(3.5, -15, None, 4.5, 255, False, 7, True))
        values = {key: item.get_value() for key, item in panel._vehicle_stats.items()}
        assert values == {'speed_kmh': '3.5', 'steering_deg': '-15', 'shift_level': '255',
                          'is_autodrive': '未使能', 'p_status': '已触发',
                          'left_wheel_kmh': '--', 'right_wheel_kmh': '4.5', 'drive_mode': '7'}
        assert panel._vehicle_stats['speed_kmh']._unit_text == 'km/h'
        assert panel._vehicle_stats['steering_deg']._unit_text == '°'
        assert '部分字段未知' in panel._vehicle_note.text()
        panel.set_vehicle_status(VehicleStatus(speed_kmh=0, p_status=False))
        assert panel._vehicle_stats['speed_kmh'].get_value() == '0'
        assert panel._vehicle_stats['p_status'].get_value() == '未触发'
        panel.clear_vehicle_status('反馈已过期')
        assert all(item.get_value() == '--' for item in panel._vehicle_stats.values())
        assert '过期' in panel._vehicle_note.text()
        panel.close()
    """)


def test_feedback_bridge_original_deadline_disconnect_and_generation(legacy_python):
    legacy_python("driving", """
        import time
        from types import SimpleNamespace
        from PyQt6.QtWidgets import QApplication
        from rd_core.wire import VehicleStatus
        from rd_driving.main_window import MainWindow
        from rd_driving.infrastructure.workers.control_system_thread import ControlSystemThread
        app = QApplication([])
        window = MainWindow()
        window._setup_control_page()
        window._setup_signals()
        window._services_running = True
        worker = ControlSystemThread(window._bridge, window.settings)
        packet = SimpleNamespace(payload=VehicleStatus(3.5, -15, None, 4.5, 2, True, 7, False),
                                 source_time_ms=1234)
        deadline = time.monotonic() + 0.15
        worker._vehicle_status(packet, deadline)
        panel = window._control_panel
        assert panel._vehicle_stats['speed_kmh'].get_value() == '3.5'
        assert window._vehicle_status_deadline == deadline
        window._vehicle_status_deadline = time.monotonic() - 1
        window._expire_telemetry()
        assert panel._vehicle_stats['speed_kmh'].get_value() == '--'
        assert '过期' in panel._vehicle_note.text()
        # Queued delivery must not give an already expired sample another budget.
        worker._vehicle_status(packet, time.monotonic() - 1)
        assert panel._vehicle_stats['speed_kmh'].get_value() == '--'
        worker._vehicle_status(packet, time.monotonic() + 0.2)
        window._on_quic_status_changed(False, '离线')
        assert panel._vehicle_stats['speed_kmh'].get_value() == '--'
        assert window._vehicle_status_deadline is None
        window._service_generation += 1
        worker._vehicle_status(packet, time.monotonic() + 0.2)
        assert panel._vehicle_stats['speed_kmh'].get_value() == '--'
        worker.request_stop()
        window.close()
        app.processEvents()
    """)
