# Standard library imports
import os
from pathlib import Path
import requests
import subprocess
import sys
import threading

# Third-party imports
from loguru import logger
from openai import OpenAI
#from docling.document_converter import DocumentConverter # Docling version
#from ollama import Client # Ollama client version

# Local imports
from app.gui import DataProcessorApp
from app.models import run_extraction_job
from app.transactions import run_transaction_job
from config import CONFIG, CONFIG_PATH, save_setting

class AppController:
    """The main controller class that orchestrates the application logic, handling both CLI and GUI modes."""

    # Accept the arguments from main.py
    def __init__(self, folder_path=None, api_choice="Mistral Cloud", model_choice="mistral-medium-latest", calc_only=False, extract_only=False) -> None:
        self.folder_path = folder_path
        self.api_choice = api_choice
        self.model_choice = model_choice
        self.calc_only = calc_only
        self.extract_only = extract_only
        self.view = None

        # Only initialize the GUI if no folder path was passed via CLI
        if not self.folder_path:
            self.view = DataProcessorApp(
                start_callback=self.handle_gui_background_worker_start,
                edit_callback=self.open_mapping_file,
                fetch_models_callback=self.fetch_custom_models
            )
            # Attach the custom log sink to send logs to the GUI
            logger.add(GUILogSink(self.view.handle_log_message))

        logger.info(f"Configuration file location: {CONFIG_PATH}")

        # Optionally check MISTRAL_API_KEY value
        if self.api_choice  == "Mistral Cloud" and not CONFIG.get("mistral_api_key") and not os.getenv("MISTRAL_API_KEY"):
            logger.warning(f"MISTRAL_API_KEY is neither defined in {CONFIG_PATH} nor in system variables. Accessing Mistral API will fail.")

    def run(self) -> None:
        """Determines the mode of operation (CLI vs GUI) and starts the appropriate workflow."""

        # If we passed a folder path via CLI, we run in CLI mode. Otherwise, we start the GUI.
        if self.folder_path:
            # CLI MODE: Run the processing directly without starting the GUI
            logger.debug(f"Running in CLI mode for folder: {self.folder_path}")

            target_folder = Path(self.folder_path)
            if not target_folder.is_dir():
                logger.error(f"Error: The path '{self.folder_path}' is not a valid directory.")
                sys.exit(1)

            if self.extract_only and self.calc_only:
                logger.error("Error: --extract-only and --calc-only cannot both be True.")
                sys.exit(1)
            run_extract = not self.calc_only
            run_calc = not self.extract_only

            self._background_worker(target_folder, self.api_choice, self.model_choice, run_extract, run_calc)
        else:
            # GUI MODE: Start the GUI and let it handle the workflow
            logger.debug("Running in GUI mode.")
            if self.view:
                self.view.protocol("WM_DELETE_WINDOW", self.on_closing)
                self.view.mainloop()

    def handle_gui_background_worker_start(self, folder_string, api_choice, model_choice, run_extract, run_calc) -> None:
        """Handles the start of processing when triggered from the GUI, passing all necessary parameters to the background worker."""
        target_folder = Path(folder_string)

        job_thread = threading.Thread(
            target=self._background_worker,
            args=(target_folder, api_choice, model_choice, run_extract, run_calc),
            daemon=True
        )
        job_thread.start()

    def _background_worker(self, folder_path, api_choice, model_choice, run_extract, run_calc) -> None:
        """The main controller module that orchestrates the application logic, handling both CLI and GUI modes."""
        try:
            summary_output = "No operations performed."

            # Extraction phase
            if run_extract:
                pdf_files = list(folder_path.glob("*.pdf"))
                total_files = len(pdf_files)

                if not total_files:
                    logger.error(f"No .pdf files found in '{folder_path}'.")
                    if self.view:
                        self.view.reset_ui()
                    return

                logger.info(f"Started processing {total_files} PDF file(s)...")
                #logger.info("Loading Docling AI models into memory... (This will only happen once)") # Docling version
                #converter = DocumentConverter() # Docling version

                for index, pdf_path in enumerate(pdf_files, start=1):
                    if self.view:
                        self.view.update_progress(pdf_path.name, index, total_files)

                    logger.info(f"Started processing file ({index}/{total_files}) '{pdf_path.name}'")
                    #run_extraction_job(pdf_path, api_choice, model_choice, converter) # Docling version
                    run_extraction_job(pdf_path, api_choice, model_choice)
                    logger.info(f"Finished processing file ({index}/{total_files})")

                logger.info("Finished extraction phase.")
            else:
                logger.info("Skipping extraction phase. Proceeding with existing JSONs.")

            # Calculation phase
            if run_calc:
                # If extraction was skipped, we jump the progress bar to 100%
                if not run_extract and self.view:
                    self.view.update_progress("Calculating transactions...", 1, 1)

                logger.info("Started transaction processing")
                summary_output = run_transaction_job(folder_path=folder_path)
                logger.info("Finished transaction processing")
            else:
                logger.info("Skipping calculation phase.")
                summary_output = "Extraction complete. Calculation phase was skipped."

            # Show summary output in the GUI or print to console if in CLI mode
            if self.view:
                self.view.after(0, self.view.show_summary, summary_output)
            else:
                print("\n--- FINAL SUMMARY ---\n" + summary_output)

        except Exception as e:
            logger.exception(f"Error during processing: {e}")
        finally:
            if self.view:
                self.view.reset_ui()
            if api_choice == "Custom Endpoint":

                try:
                    requests.post(
                        CONFIG.get("base_url", "http://localhost:11434")+"/api/generate",
                        json={"model": model_choice, "keep_alive": 0},
                        timeout=5 # Add a timeout so it doesn't hang your UI if the endpoint is offline
                    )
                    logger.info(f"Successfully unloaded {model_choice} from memory.")
                except Exception as e:
                    logger.warning(f"Failed to unload custom model: {e}")

    def fetch_custom_models(self) -> list[str]:
        """Fetches custom models using OpenAI API to populate the UI."""
        try:
            #client = Client(host=CONFIG.get("base_url", "http://localhost:11434"), timeout=3.0) # Ollama client version
            client = OpenAI(base_url=CONFIG.get("base_url", "http://localhost:11434") + "/v1", api_key="does not matter")

            # response = client.list() # Ollama client version
            response = client.models.list() # OpenAI client version

            #models = sorted([str(m.model) for m in response.models if m.model]) # Ollama client version
            models = sorted([str(m.id) for m in response.data if m.id]) # OpenAI client version

            if not models:
                logger.warning("The custom endpoint is available, but no models are present.")
                if self.view:
                    self.view.show_error_dialog(
                        "No Models Found",
                        "The custom endpoint is available, but no models are present.\nPlease pull a model (e.g., 'ollama pull ministral-3:14b' if you use Ollama) and try again."
                    )
                return []
            return models

        except Exception as e:
            logger.error(f"Failed to connect to the custom endpoint: {e}")
            if self.view:
                self.view.show_error_dialog(
                    "Offline",
                    "The custom endpoint does not appear to be available.\n\nPlease check and try again."
                )
            return []

    def open_mapping_file(self) -> None:
        """Opens the mapping CSV file in the system's default editor."""
        file_path = CONFIG.get('mapping_csv_path')

        if not file_path or not Path(file_path).exists():
            logger.warning(f"Could not find mapping file at {file_path}")
            return

        try:
            logger.debug(f"Opening {file_path}")

            if sys.platform == "linux":
                subprocess.call(["xdg-open", file_path])
            elif sys.platform == "darwin": # macOS
                subprocess.call(["open", file_path])
            elif sys.platform == "win32":
                os.startfile(file_path)
            else:
                logger.warning(f"Failed to open file: Unsupported platform {sys.platform}, trying to use xdg-open as fallback.")
                subprocess.call(["xdg-open", file_path])
        except Exception as e:
            logger.exception(f"Failed to open file: {e}")

    def on_closing(self) -> None:
        """Intercepts the window close event to save preferences."""

        if not self.view:
            return

        # Only save the model choice if Mistral Cloud is selected
        if self.view.api_choice.get() == "Mistral Cloud":
            current_model = self.view.model_choice.get().strip()
            original_model = CONFIG.get("model_choice", "")

            if current_model and current_model != original_model:
                logger.debug(f"Saving new default model choice: {current_model}")
                save_setting("model_choice", current_model)
        else:
            logger.debug("API choice is not 'Mistral Cloud'; skipping model_choice save.")

        # Save last used folder path regardless of API choice
        current_folder = self.view.selected_folder.get().strip()
        original_folder = CONFIG.get("last_folder_path", "")

        if current_folder and current_folder != original_folder:
            logger.debug(f"Saving new folder path: {current_folder}")
            save_setting("last_folder_path", current_folder)

        self.view.destroy()

class GUILogSink:
    """Custom log sink to send log messages to the GUI."""

    def __init__(self, update_gui_func):
        self.update_gui = update_gui_func

    def __call__(self, message):
        level_no = message.record["level"].no
        level_name = message.record["level"].name
        log_text = message.record["message"]

        time_str = message.record["time"].strftime("%H:%M:%S")

        # Only send INFO, WARNING, ERROR, CRITICAL to the GUI
        if level_no >= logger.level("INFO").no:
            formatted_message = f"[{time_str}] {log_text}"
            self.update_gui(formatted_message, level_name)
