import re

pattern = r'(api[_-]?key|secret|password|passwd|credential|auth[_-]?token|access[_-]?token|refresh[_-]?token)\s*[:=]\s*\S+'
replacement = r'\1=***REDACTED***'
text = 'api_key = "secret123"'

try:
    result = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    print('Result:', result)
except Exception as e:
    print('Error:', e)