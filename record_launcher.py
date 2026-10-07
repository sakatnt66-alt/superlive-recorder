import os
import re
import sys
from pathlib import Path

STREAM_NAME = os.environ.get("STREAM_NAME", "").strip()


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[^\w\u0600-\u06FF\- ]+', '', str(name or ''))
    name = re.sub(r'\s+', '_', name).strip('_')
    return name[:40]


def install_name_support():
    if not STREAM_NAME:
        return

    import record_once

    safe_name = sanitize_filename(STREAM_NAME)
    if not safe_name:
        return

    original_send = record_once.send_to_telegram_with_retry

    def wrapped_send(path, caption=""):
        path = Path(path)
        original_path = path
        target_path = path
        renamed = False

        try:
            if path.exists():
                new_name = f"{safe_name}_{path.name}"
                new_path = path.with_name(new_name)
                if new_path.exists():
                    new_path.unlink()
                path.rename(new_path)
                target_path = new_path
                renamed = True
        except Exception as e:
            try:
                record_once.log(f"Name rename warning: {e}")
            except Exception:
                pass

        escaped_name = (
            str(STREAM_NAME)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        new_caption = f"👤 <b>{escaped_name}</b>\n{caption}" if caption else f"👤 <b>{escaped_name}</b>"

        result = original_send(target_path, new_caption)

        if renamed:
            try:
                target_path.rename(original_path)
            except Exception:
                pass

        return result

    record_once.send_to_telegram_with_retry = wrapped_send


def main():
    install_name_support()

    import record_once
    import asyncio
    import traceback

    try:
        asyncio.run(record_once.main())
    except Exception as e:
        try:
            record_once.log(f"FATAL ERROR: {e}")
        except Exception:
            print(f"FATAL ERROR: {e}", flush=True)
        traceback.print_exc()
        try:
            if record_once.STREAM_ID:
                record_once.kv_update_state(
                    record_once.STREAM_ID,
                    "failed",
                    error=str(e),
                )
                record_once.kv_delete_state(record_once.STREAM_ID)
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
