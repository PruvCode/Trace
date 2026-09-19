import re

patterns = [
    # Generic key=value patterns
    (r'(api[_-]?key|secret|password|passwd|credential|auth[_-]?token|access[_-]?token|refresh[_-]?token)\s*[:=]\s*\S+', r'\1=***REDACTED***'),
    # OpenAI API keys
    (r'sk-[a-zA-Z0-9]{32,}', '***REDACTED***'),
    # Stripe keys
    (r'(sk|pk)_(live|test)_[a-zA-Z0-9]{24,}', '***REDACTED***'),
    # GitHub tokens
    (r'gh[psuo]_[a-zA-Z0-9]{36}', '***REDACTED***'),
    # Slack tokens
    (r'xox[baprs]-[\w-]{10,}', '***REDACTED***'),
    # AWS credentials
    (r'AKIA[0-9A-Z]{16}', '***REDACTED***'),
    (r'aws[_-]?secret[_-]?access[_-]?key\s*[:=]\s*\S+', r'\1=***REDACTED***'),
    # Google API keys
    (r'AIza[0-9A-Za-z\-_]{35}', '***REDACTED***'),
    # Generic bearer tokens
    (r'bearer\s+[a-zA-Z0-9\._\-]{20,}', 'bearer ***REDACTED***', re.IGNORECASE),
    # Private key blocks
    (r'-----BEGIN (RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----', '***REDACTED PRIVATE KEY***'),
    # Connection strings with passwords
    (r'(mongodb|postgres|mysql|redis)://[^:]+:[^@]+@', r'\1://***REDACTED:***REDACTED@', re.IGNORECASE),
    # JWT tokens
    (r'eyJ[a-zA-Z0-9_-]{10,}\.eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}', '***REDACTED JWT***'),
]

test_cases = [
    ('api_key = "secret123"', 0),
    ('sk-test', 1),
    ('sk_live_test', 2),
    ('ghp_test', 3),
    ('xoxb-test', 4),
    ('AKIATEST', 5),
    ('aws_secret_access_key = "secret"', 6),
    ('AIzaSyTest', 7),
    ('bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9', 8),
    ('-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQDTestKey...\n-----END PRIVATE KEY-----', 9),
    ('postgres://user:testpassword@localhost:5432/db', 10),
    ('eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.test-signature', 11),
    ('api_key = "secret123"', 0),
    ('password: "my-secret-password"', 0),
]

for i, (text, pattern_idx) in enumerate(test_cases):
    pattern, replacement = patterns[pattern_idx]
    flags = 0
    if len(patterns[pattern_idx]) == 3:
        pattern, replacement, flags = patterns[pattern_idx]
    else:
        pattern, replacement = patterns[pattern_idx]
        flags = re.IGNORECASE
    
    try:
        result = re.sub(pattern, replacement, text, flags=flags)
        print(f'Test {i} PASSED: {result[:80]}')
    except Exception as e:
        print(f'Test {i} FAILED: {e}')