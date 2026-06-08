# Standard library imports
import base64
from functools import lru_cache
import json
import os
from pathlib import Path
import time
from typing import List, Optional

# Third-party imports
import fitz
from loguru import logger
import pdfplumber
from pydantic import BaseModel, model_validator
#from docling.document_converter import DocumentConverter # Docling version

from openai import OpenAI
from openai.types.chat import ChatCompletionContentPartParam

#import ollama  # Ollama client version
from importlib.metadata import version

# Local imports
from config import CONFIG, PROMPT

# Extract the major version number to determine which Mistral client import to use
mistral_version = int(version("mistralai").split(".")[0])
if mistral_version >= 2:
    from mistralai.client import Mistral # type: ignore
else:
    from mistralai import Mistral # type: ignore

#def run_extraction_job(file_path: Path, mode, model_choice, converter: DocumentConverter) -> None: # Docling version
def run_extraction_job(file_path: Path, mode, model_choice) -> None:
    """Handles the entire extraction workflow for a single PDF file, including uploading to Mistral, processing, and cleanup."""

    if mode == "Mistral Cloud":
        sleep_in_secs = CONFIG.get("mistral_interval_sec", 30)
        logger.info(f"Waiting {sleep_in_secs} seconds to avoid rate limiting")
        time.sleep(sleep_in_secs)  # Short delay to ensure the file is fully available in Mistral's system before we attempt to access it

        #json_content = process_pdf(file_path, model_choice, converter) # Docling version
        json_content = process_pdf(file_path, model_choice)

    else:
        #json_content = process_pdf_locally(file_path, converter) # Docling version
        json_content = process_pdf_locally(file_path, model_choice)

    if json_content:
        # Create the new filename with a .json suffix
        # .with_suffix('.json') changes the extension, .name extracts just the filename (no directories)
        new_filename = file_path.with_suffix('.json').name

        # Combine the output directory with the new filename
        output_file_path = file_path.parent / new_filename

        # Write the content to the file
        with open(output_file_path, 'w', encoding='utf-8') as f:
            f.write(json_content)

        logger.info(f"Saved extracted trades to '{output_file_path.name}'")

#def process_pdf(file_path, model_choice, converter: DocumentConverter) -> str: # Docling version
def process_pdf(file_path, model_choice) -> str:
    """Handles the uploading, processing, and cleanup for a single PDF."""
    
    api_key = resolve_mistral_api_key()
    #logger.debug(f"DEBUG - Key being sent: '{api_key}' (Length: {len(api_key)})")

    if not api_key:
        logger.error("MISTRAL_API_KEY is missing! Please set it in your config file or environment variables.")
        raise ValueError("Cannot initialize Mistral client without an API key.")

    
    local_text = extract_text_locally(file_path)
    #local_text = extract_pdf_with_docling(file_path, converter) # Docling version

    try:
        # Initialize Mistral client once
        client = Mistral(api_key=api_key, timeout_ms=30_000)

        logger.info("Started uploading file to Mistral cloud for OCR processing")
        # Upload the PDF
        with open(file_path, "rb") as f:
            uploaded_file = client.files.upload(
                file={
                    "file_name": file_path.name,
                    "content": f
                },
                purpose="ocr"
            )

        # Get a signed URL for the uploaded file, handling Mistral backend race conditions
        max_retries = 3
        retry_delay = 2
        signed_url = None
        
        for attempt in range(max_retries):
            try:
                # Get a signed URL for the uploaded file (required for the OCR endpoint)
                signed_url = client.files.get_signed_url(file_id=uploaded_file.id)
                logger.info("Finished file upload and retrieved signed URL")
                break  # Exit loop if successful
            except Exception as e:
                # If we get a 404, wait and retry. Otherwise, or if out of retries, raise the error.
                if "404" in str(e) and attempt < max_retries - 1:
                    logger.warning(f"Mistral storage delay (404). Retrying in {retry_delay}s... (Attempt {attempt + 1}/{max_retries})")
                    time.sleep(retry_delay)
                else:
                    raise e
        
    # We don't want to continue processing if the upload or OCR fails (is not safe to skip a single file)
    except Exception as e:
        logger.error(f"Failed to upload file: {e}")
        raise 
    
    if not signed_url:
        raise ValueError("Signed URL was not successfully generated. Cannot proceed with OCR.")
    
    try:
        # Call the Chat Completion
        logger.info("Sent request to Mistral API")
        start_time = time.time()

        response = client.chat.parse(
            model=model_choice,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": PROMPT + f"\n=== TEXT LAYER ===\n{local_text}\n=== END ==="
                        },
                        {
                            "type": "document_url",
                            "document_url": signed_url.url}
                    ]
                }
            ],
            response_format=TradeExtraction,
            timeout_ms=300_000
        )

        end_time = time.time()
        elapsed_seconds = end_time - start_time
        logger.info(f"Received response from Mistral API in {elapsed_seconds:.2f} seconds")

        try:
            choices = getattr(response, "choices", None)
            if choices and len(choices) > 0:
                message = getattr(choices[0], "message", None)
            else:
                message = None
            if message is not None:
                validated_extraction = message.parsed  # Use already-validated Pydantic object directly from the response
            else:
                validated_extraction = None
        except (AttributeError, TypeError, IndexError):
            validated_extraction = None

        if not validated_extraction:
            raise ValueError("Mistral API failed to return a parsed structured output.")

        # Convert the cleaned Pydantic object back into a dictionary
        extracted_trades = validated_extraction.model_dump()

        # Add the filename to the root of the dictionary for traceability
        extracted_trades["filename"] = file_path.name

        return json.dumps(extracted_trades, indent=4, ensure_ascii=False)

    except Exception as e:
        logger.error(f"Error analyzing {file_path.name}: {e}")
        raise

    finally:
        # 4. Cleanup: This will ALWAYS run for each file
        logger.info("Deleting file from Mistral cloud")
        try:
            client.files.delete(file_id=uploaded_file.id)
            logger.info("Successfully deleted file from Mistral cloud")
        except Exception as e:
            logger.warning(f"Failed to delete file {uploaded_file.id} from Mistral cloud: {e}")

#def process_pdf_locally(file_path, converter: DocumentConverter) -> str: # Docling version
def process_pdf_locally(file_path, model_choice) -> str:
    """Handles local processing, and cleanup for a single PDF."""
    
    logger.debug(f"Processing: {file_path.name}")

    local_text = extract_text_locally(file_path)
    #local_text = extract_pdf_with_docling(file_path, converter) # Docling version

    pdf_document = fitz.open(file_path)

    base64_images = []
    #images_to_process = [] # Ollama client version (raw PNG bytes)

    # Convert each page of the PDF into an image (PNG format)
    for page_num in range(len(pdf_document)):
        page = pdf_document.load_page(page_num)

        # Render page to an image (pixmap)
        pix = page.get_pixmap(matrix=fitz.Matrix(1, 1)) # Higher values in the matrix increase the resolution for better OCR reading

        ## Save the rendered page image to disk for manual inspection
        #image_filename = f"{file_path.stem}_page_{page_num + 1}.png"
        #image_path = file_path.parent / image_filename
        #pix.save(image_path)
        #print(f"Saved rendered page image: {image_path}")

        # Convert the pixmap to a base64-encoded PNG string for embedding in the prompt (OpenAI client version)
        img_bytes = pix.tobytes("png")
        base64_str = base64.b64encode(img_bytes).decode('utf-8')
        base64_images.append(f"data:image/png;base64,{base64_str}")

        ## Convert to raw PNG bytes and add to our list
        # images_to_process.append(pix.tobytes("png")) # Ollama client version

    client = OpenAI(base_url=CONFIG.get("base_url", "http://localhost:11434") + "/v1", api_key="does not matter") # not needed in Ollama

    content_array: list[ChatCompletionContentPartParam] = [
            {
                "type": "text",
                "text": PROMPT + f"\n=== TEXT LAYER ===\n{local_text}\n=== END ==="
            }
        ]
    for b64_img in base64_images:
        content_array.append(
            {
                "type": "image_url",
                "image_url": {"url": b64_img}
            }
        )
    
    # Call the Chat Completion
    logger.info("Sent request to OpenAI API")
    start_time = time.time()

    response = client.chat.completions.parse(
        model=model_choice,
        messages=[{"role": "user", "content": content_array}],
        response_format=TradeExtraction # Copared to Ollama the OpenAI library can handle the Pydantic schema
    )
    
    ## Ollama client version
    #response = ollama.chat(
    #    model=model_choice
    #    messages=[
    #        {
    #            "role": "user",
    #            "content": PPROMPT + f"\n=== TEXT LAYER ===\n{local_text}\n=== END ===""",
    #            "images": images_to_process # Passing all PDF pages as a list of images
    #        }
    #    ],
    #    #Enforce the strict JSON schema output using Pydantic model
    #    format=TradeExtraction.model_json_schema()
    #)
    
    end_time = time.time()
    elapsed_seconds = end_time - start_time
    logger.info(f"Received response from OpenAI API in {elapsed_seconds:.2f} seconds")

    print("\nExtracted Transactions:")
    raw_response_content = response.choices[0].message.content # OpenAI client version
    #raw_response_content = response['message']['content'] # Ollama client version
    print(raw_response_content)

    validated_extraction = response.choices[0].message.parsed
    if validated_extraction is None:
        raise ValueError("The local model failed to return a valid parsed JSON object.")
    extracted_trades = validated_extraction.model_dump()
    #extracted_trades = json.loads(raw_response_content) # Ollama client version

    print(extracted_trades)

    try:
        # Parse it through Pydantic to trigger the math validator!
        if raw_response_content is None:
            raise ValueError("The model returned empty content.")
        validated_extraction = TradeExtraction.model_validate_json(raw_response_content)

        # Convert the cleaned Pydantic object back into a dictionary
        extracted_trades = validated_extraction.model_dump()

        # Add the filename to the root of the dictionary for traceability
        extracted_trades["filename"] = file_path.name

        return json.dumps(extracted_trades, indent=4, ensure_ascii=False)

    except Exception as e:
        logger.error(f"Failed to parse model's output into TradeExtraction schema: {e}")
        raise

def extract_text_locally(file_path) -> str:
    """Extracts the text layer from the PDF using pdfplumber so we can include it in the prompt for LLM (to guard better against OCR problems)."""
    
    full_text = ""
    logger.info("Started text layer extraction from PDF file")
    
    # Open the PDF locally
    with pdfplumber.open(file_path) as pdf:
        for i, page in enumerate(pdf.pages):
            full_text += f"\n\n--- Page {i + 1} ---\n\n"
            # Extract text while attempting to preserve the visual layout
            full_text += page.extract_text(layout=True)
    
    logger.info("Finished text layer extraction")
    return full_text

# Docling version
#def extract_pdf_with_docling(file_path, converter: DocumentConverter) -> str:
#    """Extracts text purely from the PDF text layer and formats it into Markdown preserving tables and reading order."""
#
#    logger.info("Started text layer extraction (with markdown) from PDF file")
#    # Process the document natively using the passed-in converter (Reads the text layer via pypdfium2, no OCR applied to digital text)
#    result = converter.convert(file_path)
#
#    # Export the entire document to structured Markdown
#    full_text = result.document.export_to_markdown()
#
#    # Save extracted text to .txt file
#    txt_filename = Path(file_path).with_suffix('.txt').name
#    txt_output_path = Path(file_path).parent / txt_filename
#    with open(txt_output_path, 'w', encoding='utf-8') as f:
#        f.write(full_text)
#    print(f"Saved extracted text: {txt_output_path}")
#
#    logger.info("Finished text layer extraction")
#    return full_text

@lru_cache(maxsize=1)
def resolve_mistral_api_key() -> Optional[str]:
    """ Resolves the API key exactly once. The result is cached in memory for all subsequent calls."""
    config_key = CONFIG.get("mistral_api_key")
    env_key = os.getenv("MISTRAL_API_KEY")

    # Check if both exist and whether they are conflicting
    if config_key and env_key and config_key != env_key:
        logger.warning("Different MISTRAL_API_KEY values are set in the config file and environment variables. The config file value will override the environment variable.")

    # Return config_key if it exists, otherwise fall back to env_key
    return config_key or env_key

class Trade(BaseModel):
    """Represents a single trade transaction extracted from the PDF."""
    
    position: int
    date: str
    isin: Optional[str]
    ticker: Optional[str]
    security_name: Optional[str]
    quantity: float
    price: Optional[float]
    value: Optional[float]
    fee: Optional[float]
    currency: Optional[str]
    tax_rate: float
    extra_info: Optional[str]
    original_text: str

    @model_validator(mode='after')
    def validate_and_clean_data(self) -> 'Trade':
        """Performs post-processing validation and cleaning on the extracted trade data."""

        # Value Validation: If both quantity and price are present, we calculate the value ourselves to ensure mathematical consistency
        if self.quantity is not None and self.price is not None:
            calculated_val = round(abs(self.quantity) * self.price, 4)

            # Trigger warning if the extracted value deviates significantly from the calculated value (indicating a potential extraction error)
            if self.value is not None and abs(abs(self.value) - calculated_val) > 0.05:
                logger.debug(f"Overwriting extracted net value ({self.value}) with gross math ({calculated_val})")
            self.value = calculated_val

        else:
            logger.warning(f"Missing quantity or price! Falling back to available total value: {self.value}")
            if self.value is not None:
                self.value = abs(self.value)

        # Handle null fees
        if self.fee is None:
            self.fee = 0.0
            logger.debug("Fee value was null; defaulting to 0.0")

        # Handle negative fees
        elif self.fee < 0:
            self.fee = abs(self.fee)
            logger.debug(f"Fee was negative; converted to absolute value: {self.fee}")

        # Check for unusually high fees
        max_fee = CONFIG.get("fee_threshold", 29)
        max_ratio = CONFIG.get("fee_ratio_threshold", 0.01) # IB maximum is typically 1%

        fee_ratio = (self.fee / self.value) if (self.value is not None and self.value > 0) else None
        is_high_fee = self.fee > max_fee
        is_high_ratio = fee_ratio is not None and fee_ratio > max_ratio

        if is_high_fee or is_high_ratio:
            ratio_str = f"{fee_ratio * 100:.2f}%" if fee_ratio is not None else "N/A"
            logger.warning(f"High fee detected! Fee is {self.fee} (Value: {self.value}, Ratio: {ratio_str})")
            # IB maximum (tiered account) is 29 EUR, we ignore currecies for simplicity (is only warning, not modifying the fee value)

        return self

class TradeExtraction(BaseModel):
    """Represents the structured output of the trade extraction process for a single PDF."""
    broker: Optional[str]
    trades: List[Trade]