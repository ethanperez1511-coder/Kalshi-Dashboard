FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir .

COPY src/ src/

# /data is mounted as a Railway persistent volume — DB survives redeploys.
# Set DATABASE_URL=sqlite:////data/kalshi.db in Railway env vars.
RUN mkdir -p /data

CMD ["python", "-m", "src.run_trading", "--loop", "--interval", "300"]
