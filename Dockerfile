FROM python:3.11-slim

# Real Google Chrome + a virtual screen (Xvfb): Indeed blocks headless Chrome but
# accepts a normal (headed) Chrome window, which here draws on the virtual display.
RUN apt-get update && apt-get install -y --no-install-recommends \
        wget ca-certificates xvfb xauth fonts-liberation \
    && wget -q -O /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && apt-get install -y --no-install-recommends /tmp/chrome.deb \
    && rm -rf /var/lib/apt/lists/* /tmp/chrome.deb

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# ONE worker: screening runs and the email fetch keep their progress in memory
# and run in background threads, so every request must hit the same process.
CMD xvfb-run -a --server-args="-screen 0 1280x1024x24" \
    gunicorn app:app --workers 1 --threads 8 --timeout 180 --bind 0.0.0.0:${PORT:-10000}
