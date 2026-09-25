FROM python:3.12-slim
RUN pip install --no-cache-dir vastdb
COPY setup_vastdb.py /setup_vastdb.py
ENTRYPOINT ["python", "/setup_vastdb.py"]
