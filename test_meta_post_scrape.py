from meta_scrape import get_platform, scrape_facebook, scrape_instagram

URL = "https://www.instagram.com/reel/CmUv48DLvxd/"


if __name__ == "__main__":
    platform = get_platform(URL)

    if platform == "instagram":
        image_urls = scrape_instagram(URL)
    else:
        image_urls = scrape_facebook(URL)

    print("\n--- IMAGE URLS ---")
    for i, image in enumerate(image_urls, 1):
        print(f"Image {i}: {image}")

    print(f"\nTotal image URLs found: {len(image_urls)}")
    if image_urls:
        print(f"First image URL: {image_urls[0]}")
