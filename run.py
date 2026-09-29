"""Local launcher. Configuration is read before importing the application."""
import os
from pathlib import Path
if Path('.env').exists():
    for line in Path('.env').read_text().splitlines():
        line=line.strip()
        if line and not line.startswith('#') and '=' in line:
            key,value=line.split('=',1)
            os.environ.setdefault(key.strip(),value.strip().strip('\"\''))
if __name__=='__main__':
    import uvicorn
    import app
    print('Workspace: '+os.getenv('APP_ORIGIN','http://localhost:8000'))
    print('Access token (private; paste into the workspace): '+app.TOKEN,flush=True)
    uvicorn.run(app.app,host=os.getenv('HOST','127.0.0.1'),port=int(os.getenv('PORT','8000')))
