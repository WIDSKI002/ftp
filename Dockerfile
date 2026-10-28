FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/cloudvault

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg passwd \
    && curl -fsSL https://mega.nz/linux/repo/Debian_12/Release.key \
        | gpg --dearmor -o /usr/share/keyrings/mega-archive-keyring.gpg \
    && printf '%s\n' \
        'deb [arch=amd64 signed-by=/usr/share/keyrings/mega-archive-keyring.gpg] https://mega.nz/linux/repo/Debian_12/ ./' \
        > /etc/apt/sources.list.d/mega.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends megacmd \
    && apt-get purge -y --auto-remove curl gnupg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py render_start.py ./
COPY static ./static
COPY templates ./templates

RUN useradd --create-home --home-dir /home/cloudvault --shell /usr/sbin/nologin cloudvault \
    && mkdir -p /app/instance \
    && chown -R cloudvault:cloudvault /app /home/cloudvault
USER cloudvault

CMD ["python", "render_start.py"]
