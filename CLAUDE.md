# Claude Code Telegram Bot Development Guide

This document contains development instructions and context for working with the Claude Code Telegram Bot.

## Quick Start

```bash
# Install dependencies
make dev

# Run the bot
make run

# Run in debug mode
make run-debug
```

## Testing

### Running Tests

```bash
# Run all tests
make test

# Run tests with coverage
poetry run pytest --cov=src --cov-report=html

# Run specific test file
poetry run pytest tests/unit/test_config.py

# Run tests matching a pattern
poetry run pytest -k "test_security"

# Run tests with verbose output
poetry run pytest -v
```

### Test Structure

```
tests/
├── conftest.py              # Pytest configuration and fixtures
├── unit/                    # Unit tests
│   ├── test_bot/           # Bot handler tests
│   ├── test_claude/        # Claude integration tests
│   ├── test_security/      # Security module tests
│   └── test_storage/       # Storage tests
└── integration/            # Integration tests (if any)
```

## Code Quality

### Linting and Formatting

```bash
# Check code style
make lint

# Auto-format code
make format

# Individual tools
poetry run black src tests        # Format code
poetry run isort src tests        # Sort imports
poetry run flake8 src tests       # Check style
poetry run mypy src               # Type checking
```

### Development Workflow

1. Make changes to code
2. Run tests: `make test`
3. Check code quality: `make lint`
4. Format code if needed: `make format`
5. Commit changes

## Recent Fixes & Improvements

### Configuration Validation (2025-07-14)
- **Issue**: Bot failing to start due to Pydantic validation errors for comma-separated tool lists
- **Fix**: Added field validators for `claude_allowed_tools` and `claude_disallowed_tools` in `src/config/settings.py`
- **Files**: `src/config/settings.py:163-177`

### Conversation Enhancement (2025-07-14)
- **Issue**: ConversationEnhancer method signature mismatches causing runtime errors
- **Fix**: Updated method calls in `src/bot/handlers/message.py` to match correct signatures
- **Files**: `src/bot/handlers/message.py:296-317`

### Security Middleware (2025-07-14)
- **Issue**: Overly aggressive security filtering blocking legitimate code snippets
- **Fix**: Adjusted threshold from 50% to 80% character removal AND short message length
- **Files**: `src/bot/middleware/security.py:181-205`

### Empty Message Handling (2025-07-14)
- **Issue**: Bot attempting to send empty messages causing Telegram API errors
- **Fix**: Added validation to skip empty messages before sending
- **Files**: `src/bot/handlers/message.py:263-268`

## Architecture Notes

### Key Components

- **Config**: `src/config/` - Environment-based configuration with Pydantic validation
- **Bot**: `src/bot/` - Telegram bot handlers, middleware, and features
- **Claude**: `src/claude/` - Claude AI integration (SDK and CLI modes)
- **Security**: `src/security/` - Authentication, rate limiting, and input validation
- **Storage**: `src/storage/` - SQLite database management and persistence

### Security Features

- Whitelist-based user authentication
- Path traversal prevention
- Command injection protection
- Rate limiting with token bucket algorithm
- Comprehensive audit logging

### Claude Integration

- Supports both Python SDK and CLI subprocess modes
- Session persistence and management
- Tool usage monitoring and validation
- Cost tracking per user

## Debugging

### Common Issues

1. **Bot not starting**: Check configuration validation in logs
2. **Claude errors**: Verify API key or CLI authentication
3. **Permission errors**: Check `APPROVED_DIRECTORY` path and permissions
4. **Security blocks**: Review security middleware logs for false positives

### Debug Mode

```bash
# Enable debug logging
DEBUG=true make run

# Or use debug target
make run-debug
```

### Log Locations

- Console output (structured JSON logging)
- Database audit logs in SQLite database
- Error tracking (if Sentry configured)

## Environment Setup

### Required Environment Variables

```bash
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_BOT_USERNAME=your_bot_username
APPROVED_DIRECTORY=/path/to/projects
ALLOWED_USERS=comma,separated,user,ids
```

### Optional Configuration

See `.env.example` for full configuration options including Claude settings, rate limiting, and feature flags.