"""打包入口（PyInstaller）：
- 双击 / 无子命令：有桌面环境 → 悬浮窗助手；无 tkinter → 离线雷达演示
- CLI 子命令照常可用：demo / shot / diff / watch / assistant
"""
import os
import sys
import traceback

# --noconsole 打包后 stdout/stderr 为 None，先兜底；中文 Windows 控制台是 GBK，
# print 含 ¥ 等字符的 JSON 会 UnicodeEncodeError，一并重配为 UTF-8 宽松编码
for _name in ("stdout", "stderr"):
    _s = getattr(sys, _name)
    if _s is None:
        setattr(sys, _name, open(os.devnull, "w", encoding="utf-8"))
    else:
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _install_excepthooks():
    import traceback

    def _log(exc_type, exc, tb):
        try:
            import os
            base = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, 'frozen', False) else __file__))
            with open(os.path.join(base, 'RivalEye-错误日志.txt'), 'a', encoding='utf-8') as f:
                f.write('--- 未捕获异常 ' + __import__('datetime').datetime.now().isoformat(timespec='seconds') + ' ---' + chr(10))
                traceback.print_exception(exc_type, exc, tb, file=f)
        except Exception:
            pass

    sys.excepthook = _log
    import threading
    threading.excepthook = lambda a: _log(a.exc_type, a.exc_value, a.exc_traceback)


from rival_eye.cli import main
_install_excepthooks()

if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise  # --help / 参数提示属于正常流程，不记错误日志
    except BaseException:
        # 无控制台环境下出错落日志到 exe 旁边，方便排障
        err = traceback.format_exc()
        try:
            log = os.path.join(os.path.dirname(sys.executable), "RivalEye-错误日志.txt")
            with open(log, "a", encoding="utf-8") as f:
                f.write(err + "\n")
        except OSError:
            pass
        raise
