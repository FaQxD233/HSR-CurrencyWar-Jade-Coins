"""打包入口：GitHub Actions 用 Nuitka 把它编译成 exe（见 .github/workflows/build.yml）。

exe 自带 Python 和全部依赖，并在清单里声明需要管理员权限，双击时直接弹出 UAC 确认框。
参数 --self-check 供打包流水线使用：导入全部模块、加载识别模型并实际识别一次、创建一次窗口，
成功返回 0，失败返回 1；详细结果写在 exe 旁边的 self-check.log。
"""
import os
import sys
import traceback


def self_check() -> int:
    report = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "self-check.log")
    try:
        import tkinter

        import numpy
        from PIL import Image, ImageDraw, ImageFont

        import gui  # noqa: F401  导入整条依赖链：界面、状态机、识别、Windows 平台层、热键
        import vision

        ocr = vision.Ocr()
        image = Image.new("RGB", (480, 120), "white")
        ImageDraw.Draw(image).text((20, 25), "Jade 2026", fill="black", font=ImageFont.truetype("arial.ttf", 56))
        labels = ocr.labels(numpy.asarray(image), (0, 0, 480, 120))
        if not labels:
            raise RuntimeError("识别模型没有识别出测试文字")
        templates = [vision._template(name).shape for name in ("projector.png", "jade_face.png")]
        root = tkinter.Tk()
        root.withdraw()
        root.destroy()
        with open(report, "w", encoding="utf-8") as f:
            f.write(f"ok\n识别结果：{[label.text for label in labels]}\n模板：{templates}\n")
        return 0
    except Exception:
        with open(report, "w", encoding="utf-8") as f:
            f.write(traceback.format_exc())
        return 1


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        sys.exit(self_check())
    import gui

    gui.main()
