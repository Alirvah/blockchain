#!/bin/sh
set -e

# If arguments are passed, run them directly (e.g., docker compose run web python manage.py ...)
if [ "$#" -gt 0 ]; then
    exec "$@"
fi

echo "Running migrations..."
python manage.py migrate --noinput

echo "Bootstrapping genesis..."
python manage.py bootstrap_genesis

echo "Collecting static files..."
python manage.py collectstatic --noinput

echo "Starting Gunicorn..."
exec gunicorn patcoin.wsgi:application --bind 0.0.0.0:8000 --workers 3
