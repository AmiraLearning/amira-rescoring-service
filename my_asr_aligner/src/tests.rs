#[cfg(test)]
mod tests {
    use crate::core::*;
    use proptest::prelude::*;

    #[test]
    fn test_basic_alignment() {
        let expected_items = vec!["a_letter".to_string(), "b_sound".to_string()];
        let ref_phons = vec!["a".to_string(), "bʌ".to_string()];
        let hyp_phons = vec!["ɛ".to_string(), "b".to_string()];
        let confidences = vec![0.9, 0.8];

        let result = word_level_alignment_core(expected_items, ref_phons, hyp_phons, confidences);
        assert!(result.is_ok());

        let (alignment, errors, confs) = result.unwrap();
        assert_eq!(alignment.len(), 2);
        assert_eq!(errors.len(), 2);
        assert_eq!(confs.len(), 2);

        // a_letter with ɛ should be accepted (not an error)
        assert_eq!(errors[0], false);
        // b_sound with b should be valid
        assert_eq!(errors[1], false);
    }

    #[test]
    fn test_identical_sequences() {
        let expected_items = vec!["a_letter".to_string(), "b_sound".to_string()];
        let ref_phons = vec!["a".to_string(), "bʌ".to_string()];
        let hyp_phons = vec!["a".to_string(), "bʌ".to_string()]; // Identical
        let confidences = vec![0.9, 0.8];

        let result = word_level_alignment_core(expected_items, ref_phons.clone(), hyp_phons.clone(), confidences.clone());
        assert!(result.is_ok());

        let (alignment, errors, confs) = result.unwrap();
        assert_eq!(alignment, hyp_phons);
        assert_eq!(errors, vec![false, false]);
        assert_eq!(confs, confidences);
    }

    #[test]
    fn test_error_phonemes() {
        let expected_items = vec!["a_letter".to_string()];
        let ref_phons = vec!["a".to_string()];
        let hyp_phons = vec!["x".to_string()]; // Invalid phoneme for a_letter
        let confidences = vec![0.9];

        let result = word_level_alignment_core(expected_items, ref_phons, hyp_phons, confidences);
        assert!(result.is_ok());

        let (alignment, errors, _) = result.unwrap();
        assert_eq!(alignment[0], "-");
        assert_eq!(errors[0], true);
    }

    #[test]
    fn test_empty_inputs() {
        let result = word_level_alignment_core(vec![], vec![], vec![], vec![]);
        assert!(result.is_ok());

        let (alignment, errors, confs) = result.unwrap();
        assert!(alignment.is_empty());
        assert!(errors.is_empty());
        assert!(confs.is_empty());
    }

    #[test]
    fn test_is_error_function() {
        // Valid phoneme for a_letter
        assert!(!is_error("a_letter", "a"));

        // Accepted phoneme for a_letter
        assert!(!is_error("a_letter", "ɛ"));

        // Invalid phoneme for a_letter
        assert!(is_error("a_letter", "x"));

        // Unknown item
        assert!(is_error("unknown_item", "a"));
    }


    // Property-based tests
    proptest! {
        #[test]
        fn test_alignment_never_panics(
            expected_items in prop::collection::vec(prop::string::string_regex("[a-z_]+").unwrap(), 0..10),
            ref_phons in prop::collection::vec(prop::string::string_regex("[a-zɛɑɪγʌ]+").unwrap(), 0..10),
            hyp_phons in prop::collection::vec(prop::string::string_regex("[a-zɛɑɪγʌ]+").unwrap(), 0..10),
            confidences in prop::collection::vec(0.0f32..1.0f32, 0..10)
        ) {
            let _ = word_level_alignment_core(expected_items, ref_phons, hyp_phons, confidences);
        }

        #[test]
        fn test_output_lengths_consistent(
            expected_items in prop::collection::vec(prop::string::string_regex("[a-z_]+").unwrap(), 1..20),
            ref_phons in prop::collection::vec(prop::string::string_regex("[a-zɛɑɪγʌ]+").unwrap(), 1..20),
            hyp_phons in prop::collection::vec(prop::string::string_regex("[a-zɛɑɪγʌ]+").unwrap(), 1..20),
            confidences in prop::collection::vec(0.0f32..1.0f32, 1..20)
        ) {
            if let Ok((alignment, errors, confs)) = word_level_alignment_core(expected_items, ref_phons, hyp_phons, confidences) {
                prop_assert_eq!(alignment.len(), errors.len());
                prop_assert_eq!(errors.len(), confs.len());
            }
        }
    }
}
