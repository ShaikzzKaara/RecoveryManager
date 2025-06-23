from fastapi import FastAPI, HTTPException, Request, Form
from pydantic import BaseModel
from typing import Optional, Dict, Any
import os
from dotenv import load_dotenv
from hubspot import HubSpot
from hubspot.crm.contacts import Filter, FilterGroup, PublicObjectSearchRequest
from twilio.rest import Client
import requests
import json
from datetime import datetime
import asyncio
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# Twilio configuration
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
DESTINATION_PHONE_NUMBER = os.getenv("DESTINATION_PHONE_NUMBER", "+918919025218")

# Voice configuration
VOICE_API_KEY = os.getenv("VOICE_API_KEY")
VOICE_API_URL = "https://api.ultravox.ai/api/calls"

# HubSpot configuration
HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN")


voice_call_id = ""
twilio_call_id = ""
transcription_customer_name = ""

# Validate required environment variables
required_env_vars = {
    "TWILIO_ACCOUNT_SID": TWILIO_ACCOUNT_SID,
    "TWILIO_AUTH_TOKEN": TWILIO_AUTH_TOKEN,
    "TWILIO_PHONE_NUMBER": TWILIO_PHONE_NUMBER,
    "VOICE_API_KEY": VOICE_API_KEY,
    "HUBSPOT_ACCESS_TOKEN": HUBSPOT_ACCESS_TOKEN
}

missing_vars = [var for var, value in required_env_vars.items() if not value]
if missing_vars:
    raise ValueError(f"Missing required environment variables: {', '.join(missing_vars)}")

# SYSTEM_PROMPT template
SYSTEM_PROMPT = """
# Instructions for Voice Agent
- You are {agent_name}, a professional recovery agent for {bank_name}.
- Use a professional, polite tone (e.g., 'Yaswanth' voice, en-IN).
- Replace placeholders with provided customer data.
- Deliver the message clearly, pausing briefly (1-2 seconds) between sentences.
- Detect reluctance indicators (e.g., "not now," "busy," "later," "can't talk") in customer responses.
- Keep the total message under 30 seconds unless the customer engages further.
- Log responses for feedback analysis (e.g., customer sentiment, preferred call time).
- When you say "Thank you for your time, {customer_name}. We'll follow up later. Goodbye." and the user responds with "bye" or "goodbye" (case-insensitive), pause for 2 seconds and then end the call.

# Prompt Flow
1. **Greeting**:
   "Hello, this is {bank_name} calling from the recovery department. May I speak with {customer_name}, please?"

2. **Confirm Identity**:
   - If the customer confirms their identity (e.g., "yes," "speaking," or their name), proceed to Main Message.
   - If no response or unclear response after 5 seconds, repeat the greeting once, then end with: "We'll try reaching you again later. Thank you."

3. **Main Message**:
   - In the message if there is any date or money, you should tell the date (e.g.,Twenty Second June of Two Thousand Twenty Five) or money (e.g., Fourty Hundred Rupees) like a human. 
   "{main_message}"

4. **Handle Customer Response**:
   - If the customer responds positively (e.g., "okay," "sure," "tell me more") or asks about payment:
     - Respond: "Thank you, {customer_name}. Would you like assistance with the next steps now?"
     - Wait for response (up to 3 seconds).
     - If no further engagement, end with: "Thank you for your time. Please act on this soon to avoid further action. Goodbye."
   - If the customer indicates reluctance (e.g., "not now," "busy," "call later"):
     - Respond: "I understand, {customer_name}. Could you please share a preferred time for us to call you back?"
     - Collect response (e.g., "tomorrow morning," "evening") and confirm: "Thank you, we'll call you back at {preferred_callback_time}. Have a good day."
     - Log the preferred time for feedback analysis.
   - If no response or negative response (e.g., "don't call," "not interested") after 2 seconds:
     - Respond: "Thank you for your time, {customer_name}. We'll follow up later. Goodbye."

5. **End Call**:
   - Log the interaction with customer_id, response, and any preferred call time for feedback analysis.
"""

# Pydantic models
class ContactResponse(BaseModel):
    bank_name: str
    customer_name: str
    loan_type: str
    outstanding_amount: str
    missed_emi_count: str
    emi_amount: str
    due_date: str
    proposed_months: str
    amount: str
    months: str
    phone_number: str
    call_status: str
    number_of_call_attempts: str
    call_lifted_time: str
    secure_payment_link: str
    preferred_callback_time: str
    main_message: str
    dpd_days: str
    date: str
    agent_name: str

class InitiateCallRequest(BaseModel):
    customer_name: str

class InitiateCallResponse(BaseModel):
    call_sid: str
    join_url: str
    call_id: Optional[str] = None
    message: str

class TranscriptResponse(BaseModel):
    call_id: str
    transcript: str
    message: str

class CallStatusUpdate(BaseModel):
    CallSid: Optional[str] = None
    CallStatus: Optional[str] = None
    AccountSid: Optional[str] = None
    To: Optional[str] = None
    From: Optional[str] = None
    CallDuration: Optional[str] = None
    Direction: Optional[str] = None

class CustomerData:
    """Class to handle customer data management"""
    
    def __init__(self):
        self.data = {}
    
    def create_response_object(self, contact_data: Dict[str, Any]) -> ContactResponse:
        """Create a response object from HubSpot contact data"""
        main_message = self._build_main_message(contact_data)
        
        return ContactResponse(
            customer_name=contact_data.get("customer_name", "").strip(),
            phone_number=contact_data.get("phone_number", DESTINATION_PHONE_NUMBER),
            loan_type=contact_data.get("loan_type", ""),
            outstanding_amount=contact_data.get("outstanding_amount", ""),
            missed_emi_count=contact_data.get("missed_emi_count", ""),
            emi_amount=contact_data.get("emi_amount", ""),
            due_date=contact_data.get("due_date", ""),
            dpd_days=contact_data.get("dpd_days", ""),
            bank_name=contact_data.get("bank_name", "Example Bank"),
            proposed_months=contact_data.get("proposed_months", ""),
            amount=contact_data.get("amount", ""),
            months=contact_data.get("months", ""),
            call_status=contact_data.get("call_status", ""),
            number_of_call_attempts=contact_data.get("number_of_call_attempts", ""),
            call_lifted_time=contact_data.get("call_lifted_time", ""),
            secure_payment_link=contact_data.get("secure_payment_link", "https://example.com/payment"),
            preferred_callback_time=contact_data.get("preferred_callback_time", ""),
            main_message=main_message,
            date=datetime.now().strftime("%Y-%m-%d"),
            agent_name="Yaswanth"
        )
    
    def _build_main_message(self, contact_data: Dict[str, Any]) -> str:
        """Build the main message for the voice agent"""
        customer_name = contact_data.get("customer_name", "")
        bank_name = contact_data.get("bank_name", "Example Bank")
        loan_type = contact_data.get("loan_type", "")
        outstanding_amount = contact_data.get("outstanding_amount", "")
        missed_emi_count = contact_data.get("missed_emi_count", "")
        emi_amount = contact_data.get("emi_amount", "")
        dpd_days = contact_data.get("dpd_days", "")
        due_date = contact_data.get("due_date", "")
        secure_payment_link = contact_data.get("secure_payment_link", "https://example.com/payment")
        
        return (
            f"Hello {customer_name}, this is {bank_name} regarding your {loan_type}. "
            f"Your outstanding balance is {outstanding_amount} rupees. You have missed {missed_emi_count} "
            f"EMI payments of {emi_amount} rupees each. Your account is {dpd_days} days past due as of "
            f"{due_date}. Please make a payment today via our mobile app at {secure_payment_link}."
        )

class HubSpotService:
    """Service class for HubSpot operations"""
    
    def __init__(self):
        self.client = HubSpot(access_token=HUBSPOT_ACCESS_TOKEN)
    
    async def fetch_contact(self, customer_name: str) -> Dict[str, Any]:
        """Fetch contact data from HubSpot"""
        print(f"Searching HubSpot for customer_name: {customer_name}")
        
        try:
            search_request = PublicObjectSearchRequest(
                filter_groups=[
                    FilterGroup(filters=[
                        Filter(
                            property_name="customer_name",
                            operator="EQ",
                            value=customer_name
                        )
                    ])
                ],
                properties=[
                    "bank_name", "customer_name", "loan_type",
                    "outstanding_amount", "missed_emi_count",
                    "emi_amount", "due_date", "proposed_months",
                    "amount", "months", "phone_number", "call_status",
                    "number_of_call_attempts", "call_lifted_time", "secure_payment_link",
                    "preferred_callback_time", "dpd_days", "date"
                ],
                limit=1
            )

            api_response = self.client.crm.contacts.search_api.do_search(search_request)
            print(f"HubSpot API response received")

            if not api_response.results:
                raise HTTPException(status_code=404, detail=f"No contact found for name: {customer_name}")

            return api_response.results[0].properties
            
        except Exception as e:
            logger.error(f"HubSpot Error: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Failed to fetch HubSpot data: {str(e)}")

class VoiceService:
    """Service class for Voice API operations"""
    
    async def create_call(self, customer_data: ContactResponse) -> Dict[str, Any]:
        """Create a voice call using Ultravox API"""
        print("Creating Voice call...")
        
        # Format SYSTEM_PROMPT with customer data
        formatted_prompt = SYSTEM_PROMPT.format(
            agent_name=customer_data.agent_name,
            bank_name=customer_data.bank_name,
            customer_name=customer_data.customer_name,
            main_message=customer_data.main_message,
            preferred_callback_time=customer_data.preferred_callback_time
        )
        
        voice_call_config = {
            "systemPrompt": formatted_prompt,
            "model": "fixie-ai/ultravox",
            "voice": "Yaswanth",
            "temperature": 0.3,
            "firstSpeaker": "FIRST_SPEAKER_USER",
            "medium": {"twilio": {}}
        }

        headers = {
            "Content-Type": "application/json",
            "X-API-Key": VOICE_API_KEY
        }

        try:
            response = requests.post(VOICE_API_URL, headers=headers, json=voice_call_config)
            response.raise_for_status()
            voice_response = response.json()
            call_id = voice_response.get("callId")
            print("voice call id...",call_id)
            global voice_call_id
            voice_call_id = call_id
            global transcription_customer_name
            transcription_customer_name = customer_data.customer_name
            print("Voice call created successfully")
            return voice_response
        except requests.RequestException as e:
            logger.error(f"Voice call Error: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Failed to create voice call: {str(e)}")
    
    async def fetch_transcript(self, call_id: str) -> str:
        """Fetch call transcript from Ultravox API"""
        headers = {
            "Content-Type": "application/json",
            "X-API-Key": VOICE_API_KEY
        }
        transcript_url = f"{VOICE_API_URL}/{call_id}/messages"
        print("transcript_url",transcript_url)
        
        try:
            response = requests.get(transcript_url, headers=headers)
            response.raise_for_status()
            transcript_data = response.json()
            print(f"Transcript fetched for call {call_id}")
            # print("response of transcript",transcript_data)
            return transcript_data
        except requests.RequestException as e:
            logger.error(f"Error fetching transcript for call {call_id}: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Failed to fetch transcript: {str(e)}")
        
    def save_conversations(self, transcript:Dict, customer_name:str) -> None:
        transcript_dir = "conversations"
        os.makedirs(transcript_dir,exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        transcript_file_name = f"{transcript_dir}/{customer_name}_{timestamp}.json"

        try:
            with open(transcript_file_name,'w',encoding='utf-8')as f:
                json.dump(transcript,f,indent=4)
            print(f"saved conversation to {transcript_file_name}")
        except Exception as e:
            print("Failed to save the conversation: ",e)


class TwilioService:
    """Service class for Twilio operations"""
    
    def __init__(self):
        self.client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    
    def initiate_call(self, join_url: str, phone_number: str, status_callback_url: str) -> str:
        """Initiate a Twilio call"""
        print(f"Initiating Twilio call to {phone_number}")
        
        try:
            call = self.client.calls.create(
                twiml=f'<Response><Connect><Stream url="{join_url}"/></Connect></Response>',
                to=phone_number,
                from_=TWILIO_PHONE_NUMBER,
                status_callback=status_callback_url,
                status_callback_event=["answered", "completed", "busy", "no-answer", "failed"]
            )
            print(f"Twilio call initiated with SID: {call.sid}")
            global twilio_call_id
            twilio_call_id = call.sid
            return str(call.sid)
        except Exception as e:
            logger.error(f"Error initiating Twilio call: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Failed to initiate call: {str(e)}")
    
    async def end_call(self, call_sid: str):
        """End a Twilio call"""
        try:
            call = self.client.calls(call_sid).update(status="completed")
            print(f"Twilio call {call_sid} terminated successfully")
            return call
        except Exception as e:
            logger.error(f"Error terminating Twilio call {call_sid}: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Failed to terminate call: {str(e)}")

# Initialize services
customer_data_service = CustomerData()
hubspot_service = HubSpotService()
voice_service = VoiceService()
twilio_service = TwilioService()

# FastAPI app
app = FastAPI(title="Debt Collection Voice System", version="1.0.0")

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}

@app.get("/fetch-contact", response_model=ContactResponse)
async def get_hubspot_contact(customer_name: str):
    """Fetch contact data from HubSpot"""
    if not customer_name:
        raise HTTPException(status_code=400, detail="customer_name is required")
    
    contact_data = await hubspot_service.fetch_contact(customer_name)
    return customer_data_service.create_response_object(contact_data)

@app.post("/initiate-call", response_model=InitiateCallResponse)
async def initiate_call(request: InitiateCallRequest):
    """Initiate a debt collection call"""
    print(f"Received request to initiate call for customer: {request.customer_name}")
    
    try:
        # Fetch customer data from HubSpot
        contact_data = await hubspot_service.fetch_contact(request.customer_name)
        customer_response = customer_data_service.create_response_object(contact_data)
        
        # Create Voice call
        voice_response = await voice_service.create_call(customer_response)
        join_url = voice_response.get("joinUrl")
        call_id = voice_response.get("callId")
        
        if not join_url:
            raise HTTPException(status_code=500, detail="Failed to get joinUrl from Voice call")

        # Initiate Twilio call
        # Note: Update this URL to your actual webhook endpoint
        status_callback_url = "https://1c2e-103-206-104-58.ngrok-free.app/call-status"
        call_sid = twilio_service.initiate_call(join_url, customer_response.phone_number, status_callback_url)

        return InitiateCallResponse(
            call_sid=call_sid,
            join_url=join_url,
            call_id=call_id,
            message=f"Call initiated successfully for {request.customer_name}"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error initiating call: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to initiate call: {str(e)}")

@app.post("/call-status")
async def handle_call_status(
    CallSid: str = Form(...),
    CallStatus: str = Form(...),
    AccountSid: Optional[str] = Form(None),
    To: Optional[str] = Form(None),
    From: Optional[str] = Form(None),
    CallDuration: Optional[str] = Form(None),
    Direction: Optional[str] = Form(None)
):
    """Handle call status updates from Twilio"""
    try:
        print(f"Received call status update - CallSid: {CallSid}, Status: {CallStatus}")
        
        # Handle different call statuses
        if CallStatus == "completed":
            print(f"Call {CallSid} completed successfully")
            # global voice_call_id
            print("voice call call id...",voice_call_id)
            call_transcript = await voice_service.fetch_transcript(voice_call_id)
            print("call transcript:",call_transcript)
            print("transcript_customer_name ",transcription_customer_name)
            if call_transcript.get("results"):
                voice_service.save_conversations(call_transcript,transcription_customer_name)
            else:
                print("No conversation found and saved...")
        elif CallStatus == "busy":
            print(f"Call {CallSid} was busy")
        elif CallStatus == "no-answer":
            print(f"Call {CallSid} had no answer")
        elif CallStatus == "failed":
            logger.error(f"Call {CallSid} failed")
        elif CallStatus == "answered":
            print(f"Call {CallSid} was answered")
        
        return {"message": "Status received", "status": "ok"}
        
    except Exception as e:
        logger.error(f"Error processing call status: {str(e)}")
        return {"message": "Error processing status", "error": str(e)}

@app.post("/end-call")
async def end_call_generic():
    """Generic endpoint to handle end-call requests"""
    print("Received generic end-call request")
    return {"message": "End call request received", "status": "ok"}

@app.post("/end-call/{call_sid}")
async def end_call_specific(call_sid: str):
    """End a specific call"""
    print(f"Received request to end call: {call_sid}")
    try:
        await twilio_service.end_call(call_sid)
        return {"message": f"Call {call_sid} ended successfully"}
    except Exception as e:
        logger.error(f"Error ending call {call_sid}: {str(e)}")
        return {"message": f"Error ending call: {str(e)}", "status": "error"}

@app.get("/fetch-transcript/{call_id}", response_model=TranscriptResponse)
async def get_call_transcript(call_id: str):
    """Fetch call transcript"""
    print(f"Fetching transcript for call_id: {call_id}")
    
    try:
        transcript = await voice_service.fetch_transcript(call_id)
        return TranscriptResponse(
            call_id=call_id,
            transcript=transcript,
            message=f"Transcript fetched successfully for call {call_id}"
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error fetching transcript: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch transcript: {str(e)}")
    


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
