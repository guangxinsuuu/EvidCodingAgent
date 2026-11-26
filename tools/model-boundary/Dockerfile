FROM python:3.14-slim@sha256:22eafb370920ad8b429e0aec3fff433e0669968519f5d60469d83729c138b28b
ARG OUTLET_SOURCE_SHA256
LABEL evid.model_outlet_source=$OUTLET_SOURCE_SHA256
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN mkdir /data && chown 10001:10001 /data && chmod 700 /data
COPY proxy.py /opt/outlet/proxy.py
USER 10001:10001
ENTRYPOINT ["python", "/opt/outlet/proxy.py"]
