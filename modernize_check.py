import mlflow
import requests
import time
import os

# 1. Setup Connections
MLFLOW_URI = "http://localhost:5000"
OLLAMA_URI = "http://localhost:11435/api/generate"

mlflow.set_tracking_uri(MLFLOW_URI)
# Force artifact proxy routing
os.environ["MLFLOW_TRACKING_URI"] = MLFLOW_URI

mlflow.set_experiment("Modernization_v6_5090")  # bump version again
# 2. The "Legacy" Artifact
legacy_code = """
      DO 10 I = 1, N
      SUM = SUM + A(I)
10    CONTINUE
"""


def analyze_with_gemma(code):
    prompt = f"Analyze this legacy Fortran for security vulnerabilities or optimization: {code}"
    start_time = time.time()
    try:
        response = requests.post(
            OLLAMA_URI,
            json={"model": "gemma3:27b", "prompt": prompt, "stream": False},
            timeout=300,
        )
        response.raise_for_status()
        data = response.json()
        duration = time.time() - start_time
        return data.get("response", "No response from model"), duration
    except Exception as e:
        print(f"Error talking to Ollama: {e}")
        return f"Error: {str(e)}", 0


# 3. Execution & Tracking
with mlflow.start_run(run_name="Gemma_Fortran_Analysis"):
    print("Sending code to 5090 for analysis...")
    analysis, exec_time = analyze_with_gemma(legacy_code)

    # Log Metadata
    mlflow.log_param("model", "gemma3:27b")
    mlflow.log_param("hardware", "RTX 5090")
    mlflow.log_metric("inference_time_sec", exec_time)

    # Write artifact locally first
    with open("analysis_report.txt", "w") as f:
        f.write(analysis)

    print(f"Report written, size: {os.path.getsize('analysis_report.txt')} bytes")

    # Log artifact with error surfacing
    try:
        mlflow.log_artifact("analysis_report.txt")
        print("Artifact logged successfully.")
    except Exception as e:
        print(f"Artifact upload failed: {e}")

    print(f"Done! Inference took {exec_time:.2f}s. Check MLflow UI.")
