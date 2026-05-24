FROM python:3.13-slim
RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin bingbang
WORKDIR /app
COPY --chown=bingbang:bingbang server.py index.html og.png ./
USER bingbang
EXPOSE 8000
CMD ["python", "-u", "server.py"]
