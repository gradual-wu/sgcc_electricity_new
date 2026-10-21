import os

# 国网电力官网
LOGIN_URL = "https://95598.cn/osgweb/login"
ELECTRIC_USAGE_URL = "https://95598.cn/osgweb/electricityCharge"
BALANCE_URL = "https://95598.cn/osgweb/userAcc"
PAYMENT_URL = "https://95598.cn/osgweb/paymentRecord"
BILL_SUMMARY_URL = "https://95598.cn/osgweb/electricitySummary"
STEP_URL = "https://95598.cn/osgweb/stepElectricityConsumption"

LLM_API_KEY = os.getenv('LLM_API_KEY', '').strip()
LLM_BASE_URL = os.getenv('LLM_BASE_URL', 'https://ark.cn-beijing.volces.com/api/v3')
LLM_MODEL = os.getenv('LLM_MODEL', 'doubao-seed-2-0-pro-260215')


def get_data_dir() -> str:
    """获取数据存储目录：Docker 用 /data，本地用项目下的 data/"""
    if 'PYTHON_IN_DOCKER' in os.environ:
        return '/data'
    data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data')
    os.makedirs(data_dir, exist_ok=True)
    return data_dir
