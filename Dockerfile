FROM python:3.12-slim AS vendor
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /v
RUN curl -fsSL -o leaflet.js https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.js \
 && curl -fsSL -o leaflet.css https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.css \
 && mkdir images && for f in marker-icon.png marker-icon-2x.png marker-shadow.png layers.png layers-2x.png; do \
      curl -fsSL -o images/$f https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/images/$f; done
# UI typefaces (Overpass variable + IBM Plex Mono statics), latin subset only, pinned to specific
# gstatic files so the build doesn't depend on Google Fonts' CSS API at build time.
RUN mkdir fonts \
 && curl -fsSL -o fonts/Overpass-Variable.woff2 "https://fonts.gstatic.com/l/font?kit=qFdp35WCmI96Ajtm83upeyoaX6QPnlo6_POJMrJ7bVsdpMUyQogiq00dhQ&skey=f20a355e8a5d18ab&v=v19" \
 && curl -fsSL -o fonts/PlexMono-400.woff2 https://fonts.gstatic.com/s/ibmplexmono/v20/-F63fjptAgt5VM-kVkqdyU8n1i8q1w.woff2 \
 && curl -fsSL -o fonts/PlexMono-500.woff2 https://fonts.gstatic.com/s/ibmplexmono/v20/-F6qfjptAgt5VM-kVkqdyU8n3twJwlBFgg.woff2 \
 && curl -fsSL -o fonts/PlexMono-600.woff2 https://fonts.gstatic.com/s/ibmplexmono/v20/-F6qfjptAgt5VM-kVkqdyU8n3vAOwlBFgg.woff2 \
 && curl -fsSL -o fonts/PlexMono-700.woff2 https://fonts.gstatic.com/s/ibmplexmono/v20/-F6qfjptAgt5VM-kVkqdyU8n3pQPwlBFgg.woff2

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data FRONTEND_DIR=/app/frontend
WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY backend backend
COPY frontend frontend
COPY --from=vendor /v/leaflet.js /v/leaflet.css /v/images frontend/vendor/
COPY --from=vendor /v/fonts frontend/fonts
RUN mkdir -p /data
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request as u;u.urlopen('http://127.0.0.1:8000/api/health')"
CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
