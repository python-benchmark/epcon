import uuid
from collections import namedtuple
from decimal import Decimal

from django.db.models import Max, Q
from django.db import transaction
from django.utils import timezone

from assopy.models import Order, OrderItem, Coupon, ORDER_TYPE
from conference.models import Ticket, Conference

from .fares import get_available_fares_as_dict, FareIsNotAvailable
from p3.utils import assign_ticket_to_user

ORDER_CODE_PREFIX = "O/"
ORDER_CODE_TEMPLATE = "O/%(year_two_digits)s.%(sequential_id)s"


def increment_order_code(code):
    NUMBER_OF_DIGITS_WITH_PADDING = 4

    prefix_with_year, number = code.split(".")
    number = str(int(number) + 1).zfill(NUMBER_OF_DIGITS_WITH_PADDING)
    return "{}.{}".format(prefix_with_year, number)


def latest_order_code_for_year(year):
    """
    returns latest used order.code in a given year.
    rtype – string or None
    """
    assert 2016 <= year <= 2022, year

    orders = Order.objects.filter(
        code__startswith=ORDER_CODE_PREFIX, created__year=year
    )

    return orders.aggregate(max=Max("code"))["max"]


def next_order_code_for_year(year):
    assert 2016 <= year <= 2022, year

    current_code = latest_order_code_for_year(year)
    if current_code:
        next_code = increment_order_code(current_code)
        return next_code

    # if there are no current codes, return the first one
    template = ORDER_CODE_TEMPLATE
    return template % {"year_two_digits": year % 1000, "sequential_id": "0001"}


def create_order(
    for_user, for_date, fares_info, calculation, order_type, coupon=None
):
    """
    We assume that the data passed to this function is already sanitised,
    ie. we assume that the calculation of discount is correct, and we just
    check if the discount_code is valid, w/o changing how it should apply.
    """
    assert isinstance(calculation, OrderCalculation)

    if coupon:
        assert isinstance(coupon, Coupon)

    fares = get_available_fares_as_dict(for_date)  # caching
    with transaction.atomic():
        # NOTE(artcz): Using Order().save() instead of Order.objects.create
        # because .create provides/used to privde a different API.
        # If we get rid of that dependency in other parts of the system we
        # should be able to use .create just fine.
        order = Order(
            uuid=str(uuid.uuid4()),
            user=for_user.assopy_user,
            code=next_order_code_for_year(timezone.now().year),
            order_type=order_type,
            # create a shell of an order without details
        )
        order.save()

        for fare_code, ticket_count in fares_info.items():
            fare = fares[fare_code]
            vat = fare.vat_set.all()[0]

            for i in range(ticket_count):
                # This is a relict of the past we should at some point reverse
                # the relationship and create tickets from orderitems, not the
                # other way around.
                ticket = Ticket.objects.create(user=for_user, fare=fare,
                                               name=for_user.assopy_user.name())
                assign_ticket_to_user(ticket=ticket, user=for_user)

                OrderItem.objects.create(
                    order=order,
                    code=fare_code,
                    ticket=ticket,
                    description=f"{fare.description} {i+1}/{ticket_count}",
                    # full price here, apply full discount as another OrderItem
                    price=fare.price,
                    vat=vat,
                )

        # Don't add coupon to the order if no discount is applied.
        if coupon and calculation.total_discount != 0:
            OrderItem.objects.create(
                order=order,
                # coupon=coupon,  # TODO
                ticket=None,
                code=coupon.code,
                description=f"Discount coupon {coupon.code}",
                price=Decimal(-1) * calculation.total_discount,
                # TODO/FIXME for now assuming all tickets are VAT-ed the same
                # and the discount should be VATed with the same amount as well
                vat=vat,
            )

    return order


OrderCalculation = namedtuple(
    "OrderCalculation", "final_price full_price total_discount"
)


def calculate_order_price_including_discount(
    for_user, for_date, fares_info, discount_code
):
    current_conference = Conference.objects.current()
    fares = get_available_fares_as_dict(for_date)  # caching

    try:
        coupon = Coupon.objects.get(
            Q(conference=current_conference)
            & Q(code=discount_code)
            & (Q(start_validity__lte=timezone.now().date()) | Q(start_validity=None))
            & (Q(end_validity__gte=timezone.now().date()) | Q(end_validity=None))
        )

        if not coupon.valid(user=for_user.assopy_user):
            coupon = None

    except Coupon.DoesNotExist:
        coupon = None

    full_total = 0
    for fare_code, amount_of_tickets in fares_info.items():
        if fare_code not in fares:
            raise FareIsNotAvailable(fare_code)

        full_total += fares[fare_code].price * amount_of_tickets

    if not coupon:
        return OrderCalculation(full_total, full_total, 0), coupon

    discounted_total = full_total
    # TODO: FIXME
    INFINITE_AMOUNT = 9999
    times_per_order = coupon.items_per_usage or INFINITE_AMOUNT

    coupon_applicable_fares = set(
        coupon.fares.all().values_list("code", flat=True)
    )

    for fare_code, amount_of_tickets in fares_info.items():
        if fare_code in coupon_applicable_fares:

            for i in range(amount_of_tickets):

                if times_per_order > 0:
                    discounted_price = (
                        fares[fare_code].price * coupon.discount_multiplier()
                    )
                    discounted_total -= discounted_price
                    times_per_order -= 1

    return (
        OrderCalculation(
            discounted_total, full_total, full_total - discounted_total
        ),
        coupon,
    )


def is_business_order(order):
    assert isinstance(order, Order)

    return order.order_type == ORDER_TYPE.company


def is_non_conference_ticket_order(order):
    """
    This is a bit hacky way for orders that only contain special fares, like
    Social Event tickets or sim cards (for 2019).
    This is used to check which billing form to use, and in case of `other`
    orders default to business form.
    """
    assert isinstance(order, Order)

    return order.order_type == ORDER_TYPE.other
