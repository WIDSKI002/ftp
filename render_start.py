import os
import re
import subprocess
import sys


def run_mega_command(arguments, timeout=120):
    result = subprocess.run(
        arguments,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode:
        raise RuntimeError(f"{arguments[0]} zakończyło się błędem (kod {result.returncode}).")
    return "\n".join(part for part in (result.stdout, result.stderr) if part)


def webdav_root_url(output):
    for line in output.splitlines():
        if re.match(r"^\s*/\s*:", line):
            match = re.search(r"https?://(?:127\.0\.0\.1|localhost|\[::1\]):\d+/[^\s]+", line)
            if match:
                return match.group(0)
    raise RuntimeError("MEGAcmd nie zwrócił lokalnego adresu WebDAV dla katalogu głównego.")


def configure_mega():
    if os.getenv("MEGA_ENABLED", "true").lower() != "true":
        return

    email = os.getenv("MEGA_EMAIL", "")
    password = os.getenv("MEGA_PASSWORD", "")
    if not email or not password:
        raise RuntimeError("Ustaw MEGA_EMAIL i MEGA_PASSWORD w zmiennych środowiskowych Rendera.")

    identity = subprocess.run(
        ["mega-ls", "/"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
    )
    if identity.returncode:
        run_mega_command(["mega-login", email, password])

    webdav_result = subprocess.run(
        ["mega-webdav", "/"],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    listing = subprocess.run(
        ["mega-webdav"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if listing.returncode:
        raise RuntimeError("Nie udało się odczytać konfiguracji WebDAV MEGAcmd.")

    output = "\n".join(
        part
        for part in (
            webdav_result.stdout,
            webdav_result.stderr,
            listing.stdout,
            listing.stderr,
        )
        if part
    )
    os.environ["MEGA_WEBDAV_URL"] = webdav_root_url(output)


def main():
    try:
        configure_mega()
    except (OSError, subprocess.SubprocessError, RuntimeError) as error:
        print(f"Nie udało się uruchomić integracji MEGA: {error}", file=sys.stderr)
        raise SystemExit(1) from error

    port = os.getenv("PORT", "5000")
    os.execvp(
        "gunicorn",
        [
            "gunicorn",
            "--bind",
            f"0.0.0.0:{port}",
            "--workers",
            "1",
            "--threads",
            "4",
            "--timeout",
            "120",
            "app:create_app()",
        ],
    )


if __name__ == "__main__":
    main()
