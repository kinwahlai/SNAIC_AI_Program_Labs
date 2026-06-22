"""Explore a speech Parquet file, extract WAV files, and pack audio back into Parquet.

This is an instructor demo that can later be simplified into a student exercise.

The source file stores FLAC audio bytes inside a nested Parquet column:

    audio: struct<bytes: binary, path: string>

There are two different compression layers in this example:

1. Audio compression: WAV -> FLAC
2. Parquet storage compression: column chunks -> Snappy

Run from this directory:

    python speech_parquet_exercise_demo.py

Optional arguments:

    python speech_parquet_exercise_demo.py --samples 5 --compression gzip
"""

from __future__ import annotations

import argparse
import io
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import soundfile as sf


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
    """Print file-level and row-group metadata without reading all audio bytes."""
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

    print("\nFirst five row groups:")
    for row_group_id in range(min(5, metadata.num_row_groups)):
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
            f"  {column.path_in_schema:<24} "
            f"compression={column.compression:<8} "
            f"encodings={','.join(column.encodings)}"
        )

    return parquet_file


def read_sample_records(
    parquet_file: pq.ParquetFile, sample_count: int
) -> list[dict[str, Any]]:
    """Read only a small batch for a quick classroom demonstration."""
    print_heading("STEP 2: READ A FEW RECORDS")
    columns = ["file", "audio", "text", "speaker_id", "chapter_id", "id"]
    first_batch = next(parquet_file.iter_batches(batch_size=sample_count, columns=columns))
    records = first_batch.to_pylist()

    for index, record in enumerate(records):
        audio = record["audio"]
        print(f"\nRecord {index}")
        print("  id:", record["id"])
        print("  speaker_id:", record["speaker_id"])
        print("  chapter_id:", record["chapter_id"])
        print("  embedded audio path:", audio["path"])
        print("  embedded audio bytes:", human_size(len(audio["bytes"])))
        print("  first four bytes:", audio["bytes"][:4])
        print("  text:", record["text"][:100], "...")

    print("\nThe first four bytes are b'fLaC', so the embedded audio is FLAC.")
    return records


def extract_wav_and_recompress_flac(
    records: list[dict[str, Any]], wav_dir: Path
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Decode embedded FLAC bytes to WAV files and recompress them as FLAC."""
    print_heading("STEP 3: EXTRACT WAV FILES")
    wav_dir.mkdir(parents=True, exist_ok=True)

    repacked_records: list[dict[str, Any]] = []
    summary_rows: list[dict[str, Any]] = []

    for record in records:
        source_flac = record["audio"]["bytes"]
        audio_samples, sample_rate = sf.read(
            io.BytesIO(source_flac),
            dtype="int16",
            always_2d=True,
        )

        wav_path = wav_dir / f"{record['id']}.wav"
        sf.write(wav_path, audio_samples, sample_rate, format="WAV", subtype="PCM_16")

        # Compress the WAV samples back to FLAC before storing them in Parquet.
        flac_buffer = io.BytesIO()
        sf.write(
            flac_buffer,
            audio_samples,
            sample_rate,
            format="FLAC",
            subtype="PCM_16",
        )
        recompressed_flac = flac_buffer.getvalue()

        channels = audio_samples.shape[1]
        duration_seconds = len(audio_samples) / sample_rate
        repacked_records.append(
            {
                "file": f"{record['id']}.flac",
                "audio": {
                    "bytes": recompressed_flac,
                    "path": f"{record['id']}.flac",
                },
                "text": record["text"],
                "speaker_id": record["speaker_id"],
                "chapter_id": record["chapter_id"],
                "id": record["id"],
            }
        )
        summary_rows.append(
            {
                "id": record["id"],
                "sample_rate": sample_rate,
                "channels": channels,
                "duration_seconds": round(duration_seconds, 2),
                "source_flac_size": len(source_flac),
                "wav_size": wav_path.stat().st_size,
                "recompressed_flac_size": len(recompressed_flac),
            }
        )

        print(
            f"Created {wav_path.name}: "
            f"{sample_rate} Hz, {channels} channel(s), {duration_seconds:.2f} seconds"
        )

    print("\nOpen the output folder and play the WAV files.")
    return repacked_records, summary_rows


def write_repacked_parquet(
    records: list[dict[str, Any]],
    source_schema: pa.Schema,
    output_path: Path,
    compression: str,
) -> None:
    """Write the recompressed FLAC bytes back into a small Parquet file."""
    print_heading("STEP 4: PACK THE AUDIO BACK INTO PARQUET")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    table = pa.Table.from_pylist(records, schema=source_schema)
    pq.write_table(table, output_path, compression=compression)

    print("Created:", output_path)
    print("Rows:", table.num_rows)
    print("Parquet compression:", compression)
    print("Output size:", human_size(output_path.stat().st_size))


def verify_output(output_path: Path) -> None:
    """Read the new Parquet file and verify that it contains FLAC bytes."""
    print_heading("STEP 5: VERIFY THE NEW PARQUET FILE")
    table = pq.read_table(output_path)
    first_record = table.slice(0, 1).to_pylist()[0]
    audio_bytes = first_record["audio"]["bytes"]

    print("Rows read back:", table.num_rows)
    print("Columns:", table.column_names)
    print("First record ID:", first_record["id"])
    print("First four audio bytes:", audio_bytes[:4])
    assert audio_bytes[:4] == b"fLaC"
    print("Verification passed: the packed audio is FLAC.")


def print_size_summary(summary_rows: list[dict[str, Any]]) -> None:
    print_heading("SIZE COMPARISON")
    print(
        f"{'id':<20} {'source FLAC':>12} {'WAV':>12} {'new FLAC':>12} "
        f"{'duration':>10}"
    )
    for row in summary_rows:
        print(
            f"{row['id']:<20} "
            f"{human_size(row['source_flac_size']):>12} "
            f"{human_size(row['wav_size']):>12} "
            f"{human_size(row['recompressed_flac_size']):>12} "
            f"{row['duration_seconds']:>8.2f}s"
        )

    print("\nWAV is uncompressed audio. FLAC is lossless compressed audio.")
    print("Snappy is a separate storage-layer compression codec used by Parquet.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("0000.parquet"),
        help="Input speech Parquet file (default: 0000.parquet)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("exercise_outputs"),
        help="Folder for extracted WAV files and the repacked Parquet file",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=3,
        help="Number of audio samples to extract (default: 3)",
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
    repacked_records, summary_rows = extract_wav_and_recompress_flac(
        records,
        args.output_dir / "wav",
    )
    output_path = args.output_dir / "repacked_audio_samples.parquet"
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
