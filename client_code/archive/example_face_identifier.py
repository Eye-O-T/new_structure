from face_identifier import FaceIdentifier


def main():
    identifier = FaceIdentifier(
        known_face_dir="known_faces",
        save_debug_crops=True,
    )

    result = identifier.identify_from_path(
        person_id=1,
        image_path="outputs/crops/person_1.jpg",
    )

    print(result)


if __name__ == "__main__":
    main()
