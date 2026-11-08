# SecurityLM - "Knowledge into action"

This project aims to target the privacy element of AI models where, when asking for business advice or summaries, there is a risk of leaking PII (personally identifiable information), which is not good for the company or the client. We make an easily accessible method for users to create an SLM, securing their data while receiving the help they need.

## UI

### Workspace
- Used to initialize an SLM agent based on specified parameters
- Features three SLM models of different processing speeds and power based on the task
- Allows for unique identification using text and naming features
  
### Documents
- Upload reference documents of various formats
- Redundant document collection for each profile to guarantee no cross-reference of information between profiles
- Options to select, open, replace, and delete files as needed
  
### Help & advisor
- Provides pre-generated responses to FAQs
- Allows for agentic responses for other relevant questions
  
### Assistant Setup
- Adjust premade assistant profiles
- Redefine SLM assistant parameters

## Backend
- Python used as the primary coding language
- FastAPI used to connect back-end and front end
- Qwen 2.5-1.5B-instruct used as a demo SML combined with Empire AI remote GPU as a locally hosted server
  
## Tests
- CI Pipeline used to ensure code is checked when pushed
- Comprehensive testing suite used to check key functions and security of front and back end
- Integrated testing used to test communication across modules and recreate accurate system usage

## Future development

### Improve SLM utilization
- SLMs depend on available computer resources such as GPU VRAM and RAM. These are finite in systems, so it's imperative for SLMs to be as efficient as possible to be accessible to the general populace.

### Improve company integration
- For SLMs to reach everyone, they need to be usable at all scales. This comes with being able to handle multiple people on a single SLM.

### Targeting SLMs
- For SLMs to be utilized across the board, they need to meet different requirements. This can come in the form of training data meant to target different areas and industries.
