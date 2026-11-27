FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    JAVA_HOME=/usr/lib/jvm/default-java \
    COREG_NO_BROWSE=1 COREG_RUNS_DIR=/runs \
    COREG_CHUNKED_UPLOAD=1 COREG_UPLOAD_DIR=/data/uploads

# Java (VALIS reads slides through Bio-Formats), libvips (streaming merge), and nginx (chunked upload routing)
RUN apt-get update && apt-get install -y --no-install-recommends \
      default-jre-headless libvips42 libgl1 libglib2.0-0 libgomp1 nginx \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN chmod +x /app/start.sh \
    && useradd -m app \
    && mkdir -p /data /data/uploads /runs /results \
    && chown -R app /data /runs /results
# VALIS writes bf_formats.txt (and the Bio-Formats jars) into its own install folder at first start
RUN chown -R app "$(python -c 'import valis,os;print(os.path.dirname(valis.__file__))')"
USER app

# Download the Bio-Formats jars now so the first job does not need internet. Not fatal if it fails: it then downloads on first use.
RUN python -c "from valis import slide_io; slide_io.init_jvm()" || echo "JVM warm-up skipped (jars will download on first use)"

EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
  CMD python -c "import os, urllib.request as u; port = os.environ.get('PORT', '8501'); u.urlopen(f'http://localhost:{port}/_stcore/health')" || exit 1

CMD ["/app/start.sh"]
