#!/bin/sh
set -e

PORT="${PORT:-8501}"

mkdir -p /tmp/nginx_client_body /tmp/nginx_proxy /tmp/nginx_fastcgi /tmp/nginx_uwsgi /tmp/nginx_scgi

cat > /tmp/nginx.conf <<EOF
worker_processes 1;
pid /tmp/nginx.pid;
error_log /dev/stderr warn;

events {
    worker_connections 1024;
}

http {
    include /etc/nginx/mime.types;
    default_type application/octet-stream;
    access_log /dev/stdout;

    client_body_temp_path /tmp/nginx_client_body;
    proxy_temp_path /tmp/nginx_proxy;
    fastcgi_temp_path /tmp/nginx_fastcgi;
    uwsgi_temp_path /tmp/nginx_uwsgi;
    scgi_temp_path /tmp/nginx_scgi;

    sendfile on;
    keepalive_timeout 65;

    server {
        listen ${PORT};
        server_name _;

        location /upload/ {
            proxy_pass http://127.0.0.1:8502/upload/;
            proxy_http_version 1.1;
            proxy_request_buffering off;
            client_max_body_size 100m;
            proxy_read_timeout 300s;
            proxy_send_timeout 300s;
        }

        location / {
            proxy_pass http://127.0.0.1:8503;
            proxy_http_version 1.1;
            proxy_set_header Upgrade \$http_upgrade;
            proxy_set_header Connection "upgrade";
            proxy_set_header Host \$host;
            proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto \$scheme;
            proxy_read_timeout 86400s;
            client_max_body_size 1024m;
        }
    }
}
EOF

uvicorn upload_server:app --host 127.0.0.1 --port 8502 --log-level warning &
streamlit run app.py --server.port=8503 --server.address=127.0.0.1 --server.headless=true --browser.gatherUsageStats=false &
exec nginx -c /tmp/nginx.conf -g "daemon off;"
