# Draft Copilot (ffbot) - hosted service image.
#
# Layout note: ffbot resolves its data dir relative to the package source
# (guide.py: Path(__file__).parents[1] / "data"), and the web assets in
# ffbot/web are not declared as package-data. So the app must RUN from /app
# with ffbot/ and data/ as siblings; `python -m` puts the cwd first on
# sys.path, so the copied tree wins over the site-packages install. The
# pip install below exists to pull the optional [llm] dependency
# (anthropic) - the app itself is stdlib-only.

FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY ffbot/ ./ffbot/
COPY data/guide_2026.json data/rookies_2026.json ./data/

RUN pip install --no-cache-dir ".[llm]"

# Non-root runtime user. It needs write access to /app/data because the
# SQLite db (data/ffbot.db) lives there - ephemeral by design for v1.
RUN useradd --create-home --uid 10001 ffbot \
    && chown -R ffbot:ffbot /app/data

USER ffbot

ENV FFBOT_PUBLIC=1

EXPOSE 8080

CMD ["python", "-m", "ffbot.cli", "serve", "--host", "0.0.0.0", "--port", "8080", "--public"]
