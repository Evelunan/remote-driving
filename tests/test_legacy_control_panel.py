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
        assert any('实测车速和停车反馈：尚未接入' in item.text() for item in panel.findChildren(QLabel))
        panel.clear_command_status()
        assert all(item.get_value() == '--' for item in panel._command_stats.values())
        panel.close()
    """)
