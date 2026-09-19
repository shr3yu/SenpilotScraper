from process import process_email

# Simulate an incoming email body for your technical assignment
sample_body = "Hi, can you send me the Exhibits for matter M99999?"

print("Testing process_email pipeline...")
result = process_email(sample_body)

print("\n--- Test Results ---")
print(f"Reply Text:\n{result.reply_text}")
print(f"Zip Path: {result.zip_path}")