from dotenv import load_dotenv
from anthropic import Anthropic

load_dotenv()
client = Anthropic()
print(next(b.text for b in client.messages.create(model="claude-opus-5", max_tokens=1024, messages=[{"role": "user", "content": "hello"}]).content if b.type == "text"))
