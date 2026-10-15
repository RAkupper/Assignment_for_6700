"""Train a digits classifier and save it for the prediction service."""

import argparse
from pathlib import Path

import joblib
from sklearn.datasets import load_digits
from sklearn.metrics import accuracy_score
from sklearn.model_selection import train_test_split
from sklearn.svm import SVC

DEFAULT_MODEL_PATH = Path(__file__).resolve().parents[1] / "models" / "digits.joblib"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_MODEL_PATH, help="Saved model path"
    )
    args = parser.parse_args()

    # Each image is flattened into 64 pixel values, each between 0 and 16.
    digits = load_digits()
    x_train, x_test, y_train, y_test = train_test_split(
        digits.data,
        digits.target,
        test_size=0.2,
        random_state=42,
        stratify=digits.target,
    )

    model = SVC(gamma=0.001)
    model.fit(x_train, y_train)
    accuracy = accuracy_score(y_test, model.predict(x_test))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, args.output, compress=3)

    print(f"Training samples: {len(x_train)}; test samples: {len(x_test)}")
    print(f"Held-out test accuracy: {accuracy:.2%}")
    print(f"Saved model: {args.output}")


if __name__ == "__main__":
    main()
