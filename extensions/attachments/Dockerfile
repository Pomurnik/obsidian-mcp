FROM ghcr.io/ykoellmann/obsidian-mcp@sha256:82c3892e6fc42453441a7da5cae63260c446d373a54c766e2e6e7b5c12f92cc6
COPY kb_attachments.py run.py test_extension.py /opt/kb-attachments/
ENV PYTHONPATH=/opt/kb-attachments
ENV PYTHONDONTWRITEBYTECODE=1
ENV HOME=/tmp
USER 1000:1000
RUN python -c "import importlib.metadata as m; assert m.version('obsidian-remote-mcp') == '2.1.2'"
RUN HOME=/tmp python -m unittest discover -s /opt/kb-attachments -p test_extension.py -v
ENTRYPOINT ["python", "/opt/kb-attachments/run.py"]
