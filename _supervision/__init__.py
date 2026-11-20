"""本地组件看护（逻辑层）：启动规格 spec、状态机 machine、进程记录 records、端口 ports、显存预检 gpu_preflight。

不在包初始化时导入子模块：服务端（_v3.production）只需要 gpu_preflight，而 spec 依赖 _v3 的探针，
包级导入会让两者互相等待初始化。
"""
