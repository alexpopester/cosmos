# Contact gate: forwards UDP between COSMOS and cFS only while the ground station sees the satellite
FROM python:3.12-slim
RUN pip install --no-cache-dir "skyfield==1.49" "requests>=2.31,<3"
COPY gate.py /gate.py
ENTRYPOINT ["python", "-u", "/gate.py"]
