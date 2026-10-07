import asyncio
import importlib.util
import os
import re
import sys
from pathlib import Path


def sanitize_stream_name(name: str) -> str:
    name = str(name or "").strip()
    name = re.sub(r'[^\w\u0600-\u06FF\- ]+', '', name)
    name = re.sub(r'\s+', '_', name).strip('_')
    return name[:40]


def html_escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def install_name_support(module):
    stream_name = os.environ.get("STREAM_NAME", "")
    safe_name = sanitize_stream_name(stream_name)
    display_name = str(stream_name or "").strip()[:60]

    if not safe_name:
        return

    # Patch Telegram upload: rename file and add owner name to caption.
    original_send = module.send_to_telegram_with_retry

    def wrapped_send(path, caption=""):
        original_path = Path(path)
        target_path = original_path
        renamed = False

        try:
            if original_path.exists():
                new_name = f"{safe_name}_{original_path.name}"
                new_path = original_path.with_name(new_name)

                if new_path.exists():
                    new_path.unlink()

                original_path.replace(new_path)
                target_path = new_path
                renamed = True
        except Exception as e:
            try:
                module.log(f"Name rename warning: {e}")
            except Exception:
                pass

        escaped_name = html_escape(display_name)
        new_caption = (
            f"👤 <b>{escaped_name}</b>\n{caption}"
            if caption
            else f"👤 <b>{escaped_name}</b>"
        )

        result = original_send(target_path, new_caption)

        if renamed:
            try:
                target_path.replace(original_path)
            except Exception:
                pass

        return result

    module.send_to_telegram_with_retry = wrapped_send

    # Patch normal notifications to include owner name too.
    original_notify = getattr(module, "send_telegram_notification", None)

    if callable(original_notify):
        def wrapped_notify(text):
            escaped_name = html_escape(display_name)
            return original_notify(f"👤 <b>{escaped_name}</b>\n{text}")

        module.send_telegram_notification = wrapped_notify


def load_record_once():
    record_path = Path(__file__).resolve().parent / "record_once.py"

    if not record_path.exists():
        raise RuntimeError("record_once.py not found next to record_launcher.py")

    spec = importlib.util.spec_from_file_location("record_once", record_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["record_once"] = module
    spec.loader.exec_module(module)

    return module


def main():
    module = load_record_once()
    install_name_support(module)

    try:
        asyncio.run(module.main())
    except Exception as e:
        try:
            module.log(f"FATAL ERROR: {e}")
        except Exception:
            print(f"FATAL ERROR: {e}", flush=True)

        import traceback
        traceback.print_exc()

        try:
            if module.STREAM_ID:
                module.kv_update_state(
                    module.STREAM_ID,
                    "failed",
                    error=str(e),
                )
                module.kv_delete_state(module.STREAM_ID)
        except Exception:
            pass

        sys.exit(1)


if __name__ == "__main__":
    main()
