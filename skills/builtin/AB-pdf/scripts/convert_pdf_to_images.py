import os
import sys

import pypdfium2 as pdfium

def convert(pdf_path, output_dir, max_dim=1000):
    document = pdfium.PdfDocument(pdf_path)
    if len(document) > 500:
        document.close()
        raise ValueError("PDF page limit exceeded (500)")
    os.makedirs(output_dir, exist_ok=True)
    page_count = 0
    for i, page in enumerate(document):
        image = page.render(scale=200 / 72).to_pil()
        width, height = image.size
        if width > max_dim or height > max_dim:
            scale_factor = min(max_dim / width, max_dim / height)
            new_width = int(width * scale_factor)
            new_height = int(height * scale_factor)
            image = image.resize((new_width, new_height))
        
        image_path = os.path.join(output_dir, f"page_{i+1}.png")
        image.save(image_path)
        page_count += 1
        print(f"Saved page {i+1} as {image_path} (size: {image.size})")

    document.close()
    print(f"Converted {page_count} pages to PNG images")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: convert_pdf_to_images.py [input pdf] [output directory]")
        sys.exit(1)
    pdf_path = sys.argv[1]
    output_directory = sys.argv[2]
    convert(pdf_path, output_directory)
