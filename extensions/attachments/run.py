from importlib.metadata import version

if version('obsidian-remote-mcp') != '2.1.2':
    raise RuntimeError('This extension is tested against obsidian-remote-mcp 2.1.2 only')

from kb_attachments import register

from obsidian_mcp import server

register(server)
if __name__ == '__main__':
    server.main()
