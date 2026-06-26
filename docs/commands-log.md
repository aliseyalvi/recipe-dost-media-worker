# Commands Log

Append important commands and results here after each milestone.

## Common validation

```bash
python -m compileall app
uvicorn app.main:app --reload --port 8080
curl http://127.0.0.1:8080/health