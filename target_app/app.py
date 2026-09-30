from fastapi import FastAPI

app = FastAPI(title="JenkinsGuardian Sample App")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}