"""RPGBar 服务器启动入口（PyInstaller 打包用）。

打包后由本脚本作为入口，运行 server.main.main()。
"""
from server.main import main

if __name__ == "__main__":
    main()
