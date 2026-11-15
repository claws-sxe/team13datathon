from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import boto3
import json

app = FastAPI()

# 🔓 CRUCIAL FOR HACKATHONS: Allows your React frontend to talk to this API without CORS blocks
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],      # Allows your friend's local laptop to connect
    allow_credentials=False,  # <-- CHANGE THIS TO FALSE (Fixes the fetch conflict!)
    allow_methods=["*"],
    allow_headers=["*"],
)


bedrock_client = boto3.client('bedrock-runtime', region_name='us-east-1')
lambda_client = boto3.client('lambda', region_name='us-east-1')

class ChatRequest(BaseModel):
    user_message: str
    chat_history: list = [] # Expects array of format: [{"role": "user"/"assistant", "content": "text"}]

@app.post("/api/chat")
async def chat_with_agent(request: ChatRequest):
    # Grab incoming chat history from React
    formatted_history = request.chat_history
    
    # CRUCIAL BUG FIX: Sift out any starting greeting assistant messages 
    # so the array submitted to Bedrock strictly begins with a 'user' role row
    if formatted_history and formatted_history[0]["role"] == "assistant":
        formatted_history = formatted_history[1:]
        
    messages = formatted_history + [{"role": "user", "content": [{"text": request.user_message}]}]

    
    # Keep our identical prompt rules
    system_prompt = """You are an expert AI Robo-Advisor. Extract three traits from the user: 
    1. Risk profile (low, medium, or high), 2. Horizon (in years), 3. Knowledge level.
    If metrics are found, return precisely: <trigger>{"risk_profile": "medium", "horizon_years": 5}</trigger>"""

    try:
        response = bedrock_client.converse(
            modelId="us.meta.llama3-3-70b-instruct-v1:0",
            messages=messages,
            system=[{"text": system_prompt}]
        )
        assistant_text = response['output']['message']['content']['text']
        
        # Check for our portfolio calculation trigger
        if "<trigger>" in assistant_text:
            json_str = assistant_text.split("<trigger>")[1].split("</trigger>")[0]
            params = json.loads(json_str)
            
            lambda_payload = {
                "parameters": [
                    {"name": "risk_profile", "value": params["risk_profile"]},
                    {"name": "horizon_years", "value": str(params["horizon_years"])}
                ]
            }
            
            lambda_response = lambda_client.invoke(
                FunctionName="PortfolioEngine",
                InvocationType="RequestResponse",
                Payload=json.dumps(lambda_payload)
            )
            
            math_results = json.loads(lambda_response['Payload'].read().decode('utf-8'))
            portfolio_data = math_results['functionResponse']['responseBody']['TEXT']
            
            analysis_prompt = f"The portfolio data is: {portfolio_data}. Present this breakdown cleanly and analyze 2-3 macro risk factors."
            
            final_messages = messages + [
                {"role": "assistant", "content": [{"text": "Calculating your optimal portfolio mix..."}]},
                {"role": "user", "content": [{"text": analysis_prompt}]}
            ]
            
            final_response = bedrock_client.converse(
                modelId="us.meta.llama3-3-70b-instruct-v1:0",
                messages=final_messages
            )
            return {"reply": final_response['output']['message']['content']['text'], "triggered": True}
            
        return {"reply": assistant_text, "triggered": False}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
