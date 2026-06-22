"""Explore an OCR Parquet file, extract images, and pack images back into Parquet.

This is an instructor demo that can later be simplified into a student exercise.

The source file stores JPEG image bytes inside a nested Parquet column:

    image: struct<bytes: binary, path: string>

There are two different compression layers in this example:

1. Image compression: JPEG or PNG
2. Parquet storage compression: column chunks -> Snappy

Run from this directory:

    python vision_parquet_exercise_demo.py

Optional arguments:

    python vision_parquet_exercise_demo.py --samples 5 --compression gzip
"""

from __future__ import annotations

import argparse
import io
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from PIL import Image


def human_size(size_bytes: int) -> str:
    """Return a readable file size."""
    size = float(size_bytes)
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024 or unit == "GB":
            return f"{size:.2f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


def print_heading(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def inspect_metadata(parquet_path: Path) -> pq.ParquetFile:
    """Print file-level and row-group metadata without reading every image."""
    parquet_file = pq.ParquetFile(parquet_path)
    metadata = parquet_file.metadata

    print_heading("STEP 1: READ PARQUET METADATA")
    print("File:", parquet_path)
    print("File size:", human_size(parquet_path.stat().st_size))
    print("Rows:", metadata.num_rows)
    print("Columns:", metadata.num_columns)
    print("Row groups:", metadata.num_row_groups)
    print("Created by:", metadata.created_by)
    print("\nArrow schema:")
    print(parquet_file.schema_arrow)

    print("\nRow groups:")
    for row_group_id in range(metadata.num_row_groups):
        row_group = metadata.row_group(row_group_id)
        print(
            f"  row_group={row_group_id:<2} "
            f"rows={row_group.num_rows:<4} "
            f"uncompressed_size={human_size(row_group.total_byte_size)}"
        )

    print("\nLeaf columns in row group 0:")
    first_row_group = metadata.row_group(0)
    for column_id in range(first_row_group.num_columns):
        column = first_row_group.column(column_id)
        print(
            f"  {column.path_in_schema:<20} "
            f"compression={column.compression:<8} "
            f"encodings={','.join(column.encodings)}"
        )

    return parquet_file


def read_sample_records(
    parquet_file: pq.ParquetFile, sample_count: int
) -> list[dict[str, Any]]:
    """Read only a few OCR samples for a quick classroom demonstration."""
    print_heading("STEP 2: READ A FEW RECORDS")
    first_batch = next(
        parquet_file.iter_batches(batch_size=sample_count, columns=["image", "text"])
    )
    records = first_batch.to_pylist()

    for index, record in enumerate(records):
        image_data = record["image"]
        print(f"\nRecord {index}")
        print("  embedded image path:", image_data["path"])
        print("  embedded image bytes:", human_size(len(image_data["bytes"])))
        print("  first bytes:", image_data["bytes"][:10])
        print("  OCR text:", record["text"])

    print("\nJPEG files commonly start with the bytes b'\\xff\\xd8\\xff'.")
    return records


def extract_images(
    records: list[dict[str, Any]], image_dir: Path
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Extract JPEG images, save PNG copies, and prepare records for repacking."""
    print_heading("STEP 3: EXTRACT IMAGES")
    image_dir.mkdir(parents=True, exist_ok=True)

    repacked_records: list[dict[str, Any]] = []
    summary_rows: list[dict[str, Any]] = []

    for index, record in enumerate(records):
        source_bytes = record["image"]["bytes"]
        source_name = record["image"]["path"] or f"sample-{index:04d}.jpg"
        source_path = Path(source_name)
        jpg_name = f"{index:02d}_{source_path.stem}.jpg"
        png_name = f"{index:02d}_{source_path.stem}.png"
        jpg_path = image_dir / jpg_name
        png_path = image_dir / png_name

        # The source JPEG bytes can be written directly to disk.
        jpg_path.write_bytes(source_bytes)

        # Pillow verifies that the bytes are a valid image and creates a PNG copy.
        with Image.open(io.BytesIO(source_bytes)) as image:
            image.load()
            width, height = image.size
            mode = image.mode
            image_format = image.format
            image.save(png_path, format="PNG")

        repacked_records.append(
            {
                "image": {
                    "bytes": source_bytes,
                    "path": source_name,
                },
                "text": record["text"],
            }
        )
        summary_rows.append(
            {
                "name": source_name,
                "format": image_format,
                "width": width,
                "height": height,
                "mode": mode,
                "jpg_size": jpg_path.stat().st_size,
                "png_size": png_path.stat().st_size,
            }
        )

        print(
            f"Created {jpg_name} and {png_name}: "
            f"{width}x{height}, mode={mode}, label={record['text']!r}"
        )

    print("\nOpen the output folder to view the extracted images.")
    return repacked_records, summary_rows


def write_repacked_parquet(
    records: list[dict[str, Any]],
    source_schema: pa.Schema,
    output_path: Path,
    compression: str | None,
) -> None:
    """Write the selected image records back into a small Parquet file."""
    print_heading("STEP 4: PACK THE IMAGES BACK INTO PARQUET")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    table = pa.Table.from_pylist(records, schema=source_schema)
    pq.write_table(table, output_path, compression=compression)

    print("Created:", output_path)
    print("Rows:", table.num_rows)
    print("Parquet compression:", compression or "none")
    print("Output size:", human_size(output_path.stat().st_size))


def verify_output(output_path: Path) -> None:
    """Read the new Parquet file and verify that the first JPEG is valid."""
    print_heading("STEP 5: VERIFY THE NEW PARQUET FILE")
    table = pq.read_table(output_path)
    first_record = table.slice(0, 1).to_pylist()[0]
    image_bytes = first_record["image"]["bytes"]

    with Image.open(io.BytesIO(image_bytes)) as image:
        image.verify()
        image_format = image.format

    print("Rows read back:", table.num_rows)
    print("Columns:", table.column_names)
    print("First OCR text:", first_record["text"])
    print("First image format:", image_format)
    assert image_format == "JPEG"
    print("Verification passed: the packed image is a valid JPEG.")


def print_size_summary(summary_rows: list[dict[str, Any]]) -> None:
    print_heading("SIZE COMPARISON")
    print(f"{'image':<42} {'dimensions':>12} {'JPEG':>12} {'PNG':>12}")
    for row in summary_rows:
        dimensions = f"{row['width']}x{row['height']}"
        print(
            f"{row['name'][:42]:<42} "
            f"{dimensions:>12} "
            f"{human_size(row['jpg_size']):>12} "
            f"{human_size(row['png_size']):>12}"
        )

    print("\nJPEG and PNG are image encodings. Their file sizes can differ.")
    print("Snappy is a separate storage-layer compression codec used by Parquet.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("test-00000-of-00001.parquet"),
        help="Input OCR Parquet file",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("exercise_outputs"),
        help="Folder for extracted images and the repacked Parquet file",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=3,
        help="Number of image samples to extract (default: 3)",
    )
    parser.add_argument(
        "--compression",
        choices=["snappy", "gzip", "zstd", "none"],
        default="snappy",
        help="Parquet compression codec for the repacked file (default: snappy)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.samples < 1:
        raise ValueError("--samples must be at least 1")
    if not args.input.exists():
        raise FileNotFoundError(f"Input file does not exist: {args.input.resolve()}")

    parquet_compression = None if args.compression == "none" else args.compression
    parquet_file = inspect_metadata(args.input)
    records = read_sample_records(parquet_file, args.samples)
    repacked_records, summary_rows = extract_images(
        records,
        args.output_dir / "images",
    )
    output_path = args.output_dir / "repacked_image_samples.parquet"
    write_repacked_parquet(
        repacked_records,
        parquet_file.schema_arrow,
        output_path,
        parquet_compression,
    )
    verify_output(output_path)
    print_size_summary(summary_rows)


if __name__ == "__main__":
    main()
