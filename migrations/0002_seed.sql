-- Minimal seed data for local smoke tests and Postman.
-- Safe to re-run: uses fixed UUIDs and ON CONFLICT.

INSERT INTO users (id, email, phone, push_token)
VALUES (
    '11111111-1111-1111-1111-111111111111',
    'alex@example.com',
    '+15551234567',
    'push-token-alex'
)
ON CONFLICT (id) DO NOTHING;

INSERT INTO notification_templates (category, channel, kind, subject_template, body_template)
VALUES
    ('comments', 'EMAIL', 'IMMEDIATE',
     'New comment from {{ commenter }}',
     'Hi — {{ commenter }} left a comment: {{ body }}'),
    ('comments', 'EMAIL', 'DIGEST',
     'Your comment digest ({{ events|length }} new)',
     '{% for e in events %}- {{ e.commenter }}: {{ e.body }}\n{% endfor %}'),
    ('comments', 'SMS', 'IMMEDIATE',
     '',
     'Comment from {{ commenter }}: {{ body }}'),
    ('comments', 'SMS', 'DIGEST',
     '',
     'Digest: {% for e in events %}{{ e.commenter }}; {% endfor %}'),
    ('comments', 'PUSH', 'IMMEDIATE',
     'New comment',
     '{{ commenter }}: {{ body }}'),
    ('comments', 'PUSH', 'DIGEST',
     'Comment digest',
     '{{ events|length }} new comments'),
    ('alerts', 'EMAIL', 'IMMEDIATE',
     'Alert: {{ title }}',
     '{{ message }}'),
    ('alerts', 'EMAIL', 'DIGEST',
     'Alert digest',
     '{% for e in events %}- {{ e.title }}\n{% endfor %}')
ON CONFLICT (category, channel, kind) DO NOTHING;
