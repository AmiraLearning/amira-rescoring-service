use criterion::{black_box, criterion_group, criterion_main, Criterion};
use my_asr_aligner::core::word_level_alignment_core;

fn create_test_data() -> (Vec<String>, Vec<String>, Vec<String>, Vec<f32>) {
    let expected_items = vec![
        "a_letter".to_string(), "b_sound".to_string(), "c_letter".to_string(),
        "d_sound".to_string(), "e_letter".to_string(), "f_sound".to_string(),
        "g_letter".to_string(), "h_sound".to_string(), "i_letter".to_string(),
        "j_sound".to_string(),
    ];

    let ref_phons = vec![
        "a".to_string(), "bʌ".to_string(), "si".to_string(),
        "dʌ".to_string(), "i".to_string(), "fʌ".to_string(),
        "ji".to_string(), "hʌ".to_string(), "γ".to_string(),
        "jʌ".to_string(),
    ];

    let hyp_phons = vec![
        "ɛ".to_string(), "b".to_string(), "zi".to_string(),
        "d".to_string(), "i".to_string(), "v".to_string(),
        "ji".to_string(), "h".to_string(), "ɑ".to_string(),
        "j".to_string(),
    ];

    let confidences = vec![0.9, 0.8, 0.7, 0.85, 0.95, 0.6, 0.9, 0.8, 0.7, 0.9];

    (expected_items, ref_phons, hyp_phons, confidences)
}

fn create_large_test_data() -> (Vec<String>, Vec<String>, Vec<String>, Vec<f32>) {
    let mut expected_items = Vec::new();
    let mut ref_phons = Vec::new();
    let mut hyp_phons = Vec::new();
    let mut confidences = Vec::new();

    let phonemes = [
        ("a_letter", "a", "ɛ", 0.8),
        ("b_sound", "bʌ", "b", 0.9),
        ("c_letter", "si", "zi", 0.7),
        ("d_sound", "dʌ", "d", 0.85),
        ("e_letter", "i", "i", 0.95),
        ("f_sound", "fʌ", "v", 0.6),
    ];

    // Create 100 repetitions for larger test
    for _ in 0..100 {
        for &(expected, ref_ph, hyp_ph, conf) in &phonemes {
            expected_items.push(expected.to_string());
            ref_phons.push(ref_ph.to_string());
            hyp_phons.push(hyp_ph.to_string());
            confidences.push(conf);
        }
    }

    (expected_items, ref_phons, hyp_phons, confidences)
}

fn bench_small_alignment(c: &mut Criterion) {
    let (expected_items, ref_phons, hyp_phons, confidences) = create_test_data();

    c.bench_function("small_alignment", |b| {
        b.iter(|| {
            word_level_alignment_core(
                black_box(expected_items.clone()),
                black_box(ref_phons.clone()),
                black_box(hyp_phons.clone()),
                black_box(confidences.clone()),
            ).unwrap()
        })
    });
}

fn bench_large_alignment(c: &mut Criterion) {
    let (expected_items, ref_phons, hyp_phons, confidences) = create_large_test_data();

    c.bench_function("large_alignment", |b| {
        b.iter(|| {
            word_level_alignment_core(
                black_box(expected_items.clone()),
                black_box(ref_phons.clone()),
                black_box(hyp_phons.clone()),
                black_box(confidences.clone()),
            ).unwrap()
        })
    });
}

fn bench_identical_sequences(c: &mut Criterion) {
    let expected_items = vec!["a_letter".to_string(); 50];
    let ref_phons = vec!["a".to_string(); 50];
    let hyp_phons = vec!["a".to_string(); 50];  // Identical to ref_phons
    let confidences = vec![0.9; 50];

    c.bench_function("identical_sequences", |b| {
        b.iter(|| {
            word_level_alignment_core(
                black_box(expected_items.clone()),
                black_box(ref_phons.clone()),
                black_box(hyp_phons.clone()),
                black_box(confidences.clone()),
            ).unwrap()
        })
    });
}

criterion_group!(benches, bench_small_alignment, bench_large_alignment, bench_identical_sequences);
criterion_main!(benches);
