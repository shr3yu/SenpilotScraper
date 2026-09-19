from process import process_email

# Happy path to test the process_email function with a sample email body.
sample_body = "Hi, could you please send me the Exhibits for matter M12205? Thanks"

"""
# Missing doc type
sample_body = "Hi, I need documents for matter M12205 please.\nThanks"

# Missing matter number
sample_body = "Hi, could you send me the Exhibits please?\nThanks" 

# Invalid matter number
sample_body = "Hi, could you send me the Exhibits for matter M99999?\nThanks"
"""

print("Testing process_email pipeline...")
result = process_email(sample_body)

print("\n--- Test Results ---")
print(f"Reply Text:\n{result.reply_text}")
print(f"Zip Path: {result.zip_path}")